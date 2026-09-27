"""
Bulk restore of objects to selected historical versions.

The entry point is ``plan_bulk_restore()``, which takes a list of history ids
and builds a checkable :class:`RestorePlan`. The plan lists the objects that
will be created, updated and whose relations will be replaced, as well as the
reasons that block the restore. A plan without blockers can be applied
atomically with :meth:`RestorePlan.apply`; the whole plan either succeeds or
is rolled back.
"""

from dataclasses import dataclass, field as dataclass_field
from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import transaction
from django.db.models import ForeignKey, ManyToManyField

from . import utils
from .exceptions import RestoreBlockedError, RestoreConflictError

# Blocker codes
HISTORY_NOT_FOUND = "history_not_found"
DUPLICATE_VERSION = "duplicate_version"
FIELD_REMOVED = "field_removed"
FK_TARGET_DELETED = "fk_target_deleted"

# Action types
ACTION_CREATE = "create"
ACTION_UPDATE = "update"


@dataclass
class FieldChange:
    """A single scalar field that will be restored to a historical value."""

    field: str
    current: Any
    target: Any


@dataclass
class M2MRelationChange:
    """An M2M relation whose rows will be replaced by the historical rows."""

    field: str
    current: list
    target: list


@dataclass
class RestoreBlocker:
    """A reason why a single history record cannot be restored."""

    history_id: Any
    code: str
    message: str


@dataclass
class _M2MRestoreSpec:
    """Internal bookkeeping for restoring one tracked M2M field."""

    field: ManyToManyField
    through: Any
    own_attname: str  # through FK pointing back at the restored model
    target_attname: str  # through FK pointing at the related model
    target_rows: list  # through-row dicts (without through pk) to recreate


@dataclass
class RestoreAction:
    """The restore of a single object to one of its historical versions."""

    history_id: Any
    model: Any
    instance_pk: Any
    action: str  # ACTION_CREATE or ACTION_UPDATE
    field_changes: list = dataclass_field(default_factory=list)
    m2m_changes: list = dataclass_field(default_factory=list)

    # Internal state used when applying the plan.
    history_record: Any = None
    _expected_values: dict | None = None
    _expected_m2m: dict = dataclass_field(default_factory=dict)
    _m2m_specs: list = dataclass_field(default_factory=list)


class RestorePlan:
    """
    A checkable plan for restoring a set of objects to historical versions.

    ``actions`` lists every object that will be created, updated or have its
    relations replaced; ``blockers`` lists every reason the plan cannot run.
    A plan with blockers cannot be applied.
    """

    def __init__(self, history_model):
        self.history_model = history_model
        self.model = history_model.instance_type
        self.actions = []
        self.blockers = []

    @property
    def ok(self):
        return not self.blockers

    @property
    def creates(self):
        """Actions that will re-create a currently missing object."""
        return [a for a in self.actions if a.action == ACTION_CREATE]

    @property
    def updates(self):
        """Actions that will change scalar fields of an existing object."""
        return [a for a in self.actions if a.field_changes]

    @property
    def relation_replacements(self):
        """Actions that will replace M2M relations of an object."""
        return [a for a in self.actions if a.m2m_changes]

    def apply(self, user=None, change_reason=None):
        """
        Atomically apply the whole plan to the current business table.

        The current state of every object is re-checked against the state the
        plan was generated from; if anything has drifted, RestoreConflictError
        is raised and nothing is applied. Every restored object gets a new
        history record whose change reason references the source history id;
        the original history is never modified.
        """
        if self.blockers:
            raise RestoreBlockedError(
                "Restore plan is blocked: "
                + "; ".join(b.message for b in self.blockers)
            )
        with transaction.atomic():
            for action in self.actions:
                _verify_current_state(self.model, action)
            for action in self.actions:
                _apply_action(
                    self.model, action, user=user, change_reason=change_reason
                )
        return self


def _tracked_model_fields(history_model):
    """Tracked history fields that still exist on the current model."""
    model = history_model.instance_type
    existing, missing = [], []
    for history_field in history_model.tracked_fields:
        try:
            model_field = model._meta.get_field(history_field.name)
        except FieldDoesNotExist:
            missing.append(history_field.name)
        else:
            existing.append((history_field, model_field))
    return existing, missing


def _through_rows(through, own_attname, instance_pk, attnames):
    """Sorted tuples describing the current through rows of one object."""
    rows = (
        through._default_manager.filter(**{own_attname: instance_pk})
        .values_list(*attnames)
        .order_by(*attnames)
    )
    return [tuple(row) for row in rows]


def _m2m_specs_for_record(history_model, record):
    """
    Build the restore specs for every tracked M2M field of one history record,
    reading the target through rows from the M2M history models.
    """
    specs = []
    for m2m_field in history_model._history_m2m_fields:
        through = m2m_field.remote_field.through
        own_name = utils.get_m2m_field_name(m2m_field)
        target_name = utils.get_m2m_reverse_field_name(m2m_field)
        own_attname = through._meta.get_field(own_name).attname
        target_attname = through._meta.get_field(target_name).attname
        through_pk_name = through._meta.pk.name
        m2m_history_model = getattr(history_model, m2m_field.name).model
        target_rows = []
        for row in m2m_history_model._default_manager.filter(history_id=record.pk):
            values = {}
            for through_field in through._meta.fields:
                if through_field.name == through_pk_name:
                    continue
                values[through_field.attname] = getattr(
                    row, through_field.attname
                )
            target_rows.append(values)
        specs.append(
            _M2MRestoreSpec(
                field=m2m_field,
                through=through,
                own_attname=own_attname,
                target_attname=target_attname,
                target_rows=target_rows,
            )
        )
    return specs


def plan_bulk_restore(history_ids, history_model):
    """
    Build a RestorePlan for the given history ids of one history model.

    The plan is only a description - nothing is written to the database.
    Blocking problems (unknown history ids, two versions of the same object,
    history fields that no longer exist on the current model, foreign key
    targets that have been deleted) are collected in ``plan.blockers``.
    """
    # Deduplicate while preserving order
    history_ids = list(dict.fromkeys(history_ids))
    plan = RestorePlan(history_model)
    model = plan.model
    history_pk = history_model._meta.pk.attname
    obj_pk_attname = utils.get_app_model_primary_key_name(model)

    records = {
        getattr(record, history_pk): record
        for record in history_model._default_manager.filter(
            **{f"{history_pk}__in": history_ids}
        )
    }
    for history_id in history_ids:
        if history_id not in records:
            plan.blockers.append(
                RestoreBlocker(
                    history_id,
                    HISTORY_NOT_FOUND,
                    f"History record {history_id!r} does not exist.",
                )
            )

    # The same object must not be restored to two different versions
    versions_per_object = {}
    for history_id, record in records.items():
        versions_per_object.setdefault(getattr(record, obj_pk_attname), []).append(
            history_id
        )
    for object_pk, ids in versions_per_object.items():
        if len(ids) > 1:
            for history_id in ids:
                plan.blockers.append(
                    RestoreBlocker(
                        history_id,
                        DUPLICATE_VERSION,
                        f"Object {object_pk!r} has {len(ids)} versions selected "
                        f"({ids}); only one version per object can be restored.",
                    )
                )

    tracked_fields, missing_fields = _tracked_model_fields(history_model)
    if missing_fields:
        for history_id in records:
            plan.blockers.append(
                RestoreBlocker(
                    history_id,
                    FIELD_REMOVED,
                    f"History fields no longer present on "
                    f"{model._meta.label}: {sorted(missing_fields)}.",
                )
            )

    blocked_ids = {blocker.history_id for blocker in plan.blockers}
    candidates = [
        (history_id, records[history_id])
        for history_id in history_ids
        if history_id in records and history_id not in blocked_ids
    ]

    # Check that every foreign key target referenced by the selected history
    # records (scalar FKs and M2M targets) still exists.
    fk_fields = [
        (history_field, model_field)
        for history_field, model_field in tracked_fields
        if isinstance(model_field, ForeignKey)
    ]
    fk_targets = {}  # related model -> set of pks referenced
    for _, record in candidates:
        for history_field, model_field in fk_fields:
            target_pk = getattr(record, history_field.attname)
            if target_pk is not None:
                related = model_field.remote_field.model
                fk_targets.setdefault(related, set()).add(target_pk)

    m2m_specs = {}
    for history_id, record in candidates:
        specs = _m2m_specs_for_record(history_model, record)
        m2m_specs[history_id] = specs
        for spec in specs:
            related = spec.field.remote_field.model
            for row in spec.target_rows:
                fk_targets.setdefault(related, set()).add(row[spec.target_attname])

    missing_targets = set()
    for related, pks in fk_targets.items():
        related_pk = related._meta.pk.attname
        existing = set(
            related._default_manager.filter(**{f"{related_pk}__in": pks}).values_list(
                related_pk, flat=True
            )
        )
        for pk in pks - existing:
            missing_targets.add((related, pk))

    for history_id, record in candidates:
        for history_field, model_field in fk_fields:
            target_pk = getattr(record, history_field.attname)
            if target_pk is None:
                continue
            related = model_field.remote_field.model
            if (related, target_pk) in missing_targets:
                plan.blockers.append(
                    RestoreBlocker(
                        history_id,
                        FK_TARGET_DELETED,
                        f"Foreign key target {related._meta.label} "
                        f"pk={target_pk!r} referenced by field "
                        f"'{model_field.name}' has been deleted.",
                    )
                )
        for spec in m2m_specs[history_id]:
            related = spec.field.remote_field.model
            missing_pks = sorted(
                row[spec.target_attname]
                for row in spec.target_rows
                if (related, row[spec.target_attname]) in missing_targets
            )
            if missing_pks:
                plan.blockers.append(
                    RestoreBlocker(
                        history_id,
                        FK_TARGET_DELETED,
                        f"M2M field '{spec.field.name}' targets of "
                        f"{related._meta.label} have been deleted: "
                        f"{missing_pks}.",
                    )
                )

    blocked_ids = {blocker.history_id for blocker in plan.blockers}

    # Snapshot the current state of every restorable object; the apply step
    # re-checks these snapshots to detect drift.
    object_pks = {
        getattr(record, obj_pk_attname)
        for history_id, record in candidates
        if history_id not in blocked_ids
    }
    value_attnames = [model_field.attname for _, model_field in tracked_fields]
    if obj_pk_attname not in value_attnames:
        value_attnames.append(obj_pk_attname)
    current_values = {}
    if object_pks and value_attnames:
        for row in model._default_manager.filter(
            **{f"{obj_pk_attname}__in": object_pks}
        ).values(*value_attnames):
            current_values[row[obj_pk_attname]] = row

    for history_id, record in candidates:
        if history_id in blocked_ids:
            continue
        object_pk = getattr(record, obj_pk_attname)
        expected_values = current_values.get(object_pk)
        action = RestoreAction(
            history_id=history_id,
            model=model,
            instance_pk=object_pk,
            action=ACTION_CREATE if expected_values is None else ACTION_UPDATE,
            history_record=record,
            _expected_values=expected_values,
        )
        for history_field, model_field in tracked_fields:
            target = getattr(record, history_field.attname)
            current = (
                expected_values.get(model_field.attname)
                if expected_values is not None
                else None
            )
            if current != target:
                action.field_changes.append(
                    FieldChange(field=model_field.name, current=current, target=target)
                )
        for spec in m2m_specs[history_id]:
            through_attnames = [
                f.attname for f in spec.through._meta.fields
            ]
            expected_rows = (
                _through_rows(
                    spec.through, spec.own_attname, object_pk, through_attnames
                )
                if expected_values is not None
                else []
            )
            action._expected_m2m[spec.field.name] = expected_rows
            current_targets = sorted(
                row[through_attnames.index(spec.target_attname)]
                for row in expected_rows
            )
            target_targets = sorted(
                row[spec.target_attname] for row in spec.target_rows
            )
            if current_targets != target_targets:
                action.m2m_changes.append(
                    M2MRelationChange(
                        field=spec.field.name,
                        current=current_targets,
                        target=target_targets,
                    )
                )
            action._m2m_specs.append(spec)
        plan.actions.append(action)

    return plan


def _verify_current_state(model, action):
    """Re-check that the object has not drifted since the plan was generated."""
    obj_pk_attname = utils.get_app_model_primary_key_name(model)
    if action._expected_values is None:
        exists = model._default_manager.filter(
            **{obj_pk_attname: action.instance_pk}
        ).exists()
        if exists:
            raise RestoreConflictError(
                f"{model._meta.label} pk={action.instance_pk!r} was created "
                "after the restore plan was generated."
            )
        return
    attnames = list(action._expected_values.keys())
    current = (
        model._default_manager.filter(**{obj_pk_attname: action.instance_pk})
        .values(*attnames)
        .first()
    )
    if current is None:
        raise RestoreConflictError(
            f"{model._meta.label} pk={action.instance_pk!r} was deleted "
            "after the restore plan was generated."
        )
    if dict(current) != dict(action._expected_values):
        raise RestoreConflictError(
            f"{model._meta.label} pk={action.instance_pk!r} has changed "
            "since the restore plan was generated."
        )
    for spec in action._m2m_specs:
        through_attnames = [f.attname for f in spec.through._meta.fields]
        current_rows = _through_rows(
            spec.through, spec.own_attname, action.instance_pk, through_attnames
        )
        if current_rows != action._expected_m2m[spec.field.name]:
            raise RestoreConflictError(
                f"M2M field '{spec.field.name}' of {model._meta.label} "
                f"pk={action.instance_pk!r} has changed since the restore "
                "plan was generated."
            )


def _apply_action(model, action, user=None, change_reason=None):
    record = action.history_record
    if action.action == ACTION_CREATE:
        instance = model(
            **{utils.get_app_model_primary_key_name(model): action.instance_pk}
        )
    else:
        instance = model._default_manager.get(
            **{utils.get_app_model_primary_key_name(model): action.instance_pk}
        )
    tracked_fields, _ = _tracked_model_fields(type(record))
    for history_field, model_field in tracked_fields:
        setattr(instance, model_field.attname, getattr(record, history_field.attname))
    # Persist the scalar values without writing history yet, so that the M2M
    # snapshot taken by the final save reflects the restored relations.
    instance.save_without_historical_record()

    for spec in action._m2m_specs:
        spec.through._default_manager.filter(
            **{spec.own_attname: action.instance_pk}
        ).delete()
        spec.through._default_manager.bulk_create(
            [spec.through(**row) for row in spec.target_rows]
        )

    if change_reason is None:
        change_reason = f"Restored from history_id={action.history_id}"
    instance._change_reason = change_reason
    if user is not None:
        instance._history_user = user
    # This save creates exactly one new history record (including the M2M
    # snapshot) carrying the source history id in its change reason.
    instance.save()


def bulk_restore(history_ids, history_model, user=None, change_reason=None):
    """
    Plan and atomically apply a bulk restore in one step.

    Raises RestoreBlockedError if the generated plan has any blockers.
    """
    plan = plan_bulk_restore(history_ids, history_model)
    plan.apply(user=user, change_reason=change_reason)
    return plan
