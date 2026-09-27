"""
Utilities for scrubbing field values out of historical records.

Unlike reverting or deleting history, this rewrites selected tracked fields
to a fixed replacement value while leaving the audit identity of every
record (``history_id``, ``history_date``, history user, ...) untouched.
"""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q

from simple_history.exceptions import NotHistoricalModelError
from simple_history.utils import get_history_model_for_model


class HistoryFieldCleanupError(ValueError):
    """Raised when a history field cleanup request is invalid."""


@dataclass(frozen=True)
class HistoryFieldCleanupSpec:
    """Validated description of a history field cleanup operation.

    Instances must be created with :func:`build_cleanup_spec`, which performs
    all of the pre-flight validation.
    """

    model: type
    history_model: type
    fields: tuple
    object_ids: tuple
    replacement_value: object
    set_null: bool
    cutoff_history_id: object
    resume_after_history_id: object


@dataclass
class HistoryFieldCleanupBatch:
    """Progress information yielded after each processed batch."""

    batch_number: int
    rows_in_scope: int
    rows_changed: int
    last_history_id: object


@dataclass
class HistoryFieldCleanupResult:
    """Summary of a completed cleanup run."""

    cutoff_history_id: object
    rows_in_scope: int = 0
    rows_changed: int = 0
    batches: int = 0
    last_history_id: object = None


def get_protected_history_fields(history_model):
    """Return names of audit/identity fields that must never be rewritten."""
    names = {
        "history_id",
        "history_date",
        "history_type",
        "history_user_id",
        "history_user",
        "history_change_reason",
        "history_relation",
    }
    pk = history_model._meta.pk
    if pk is not None:
        names.add(pk.name)
        names.add(pk.attname)
    return names


def _cleaned_fields(history_model, field_names):
    fields = []
    seen = set()
    for field_name in field_names:
        try:
            field = history_model._meta.get_field(field_name)
        except Exception:
            raise HistoryFieldCleanupError(
                f"Field '{field_name}' does not exist on "
                f"'{history_model._meta.label}'."
            )
        if field.name in seen:
            raise HistoryFieldCleanupError(
                f"Field '{field.name}' was specified more than once."
            )
        seen.add(field.name)
        fields.append(field)
    return fields


def _coerce_replacement_value(field, raw_value):
    """Validate/convert the user supplied replacement value for ``field``."""
    try:
        value = field.to_python(raw_value)
    except (TypeError, ValueError, ValidationError) as error:
        raise HistoryFieldCleanupError(
            f"Replacement value for '{field.name}' is not compatible with "
            f"{field.__class__.__name__}: {error}"
        )
    max_length = getattr(field, "max_length", None)
    if max_length is not None and value is not None and len(value) > max_length:
        raise HistoryFieldCleanupError(
            f"Replacement value for '{field.name}' exceeds max_length "
            f"({len(value)} > {max_length})."
        )
    return value


def _validate_target_field(field):
    protected = get_protected_history_fields(field.model)
    if field.name in protected or field.attname in protected:
        raise HistoryFieldCleanupError(
            f"Refusing to modify audit identity field '{field.name}'."
        )
    if getattr(field, "primary_key", False):
        raise HistoryFieldCleanupError(
            f"Refusing to modify primary key field '{field.name}'."
        )
    if field.is_relation:
        raise HistoryFieldCleanupError(
            f"Field '{field.name}' is a relation field; relation fields "
            "cannot be rewritten by this tool."
        )
    if isinstance(field, models.ManyToManyField):
        raise HistoryFieldCleanupError(
            f"Field '{field.name}' is a many-to-many field and cannot "
            "be rewritten."
        )


def _validate_nullable(field, set_null):
    if set_null and not field.null:
        raise HistoryFieldCleanupError(
            f"Field '{field.name}' is not nullable, so it cannot be set "
            "to NULL. Provide a replacement value instead."
        )


def _parse_object_ids(model, object_ids):
    ids = []
    seen = set()
    for raw_id in object_ids:
        try:
            parsed_id = model._meta.pk.to_python(raw_id)
        except (TypeError, ValueError, ValidationError) as error:
            raise HistoryFieldCleanupError(
                f"Invalid object id '{raw_id}' for {model._meta.label}: {error}"
            )
        if parsed_id in seen:
            continue
        seen.add(parsed_id)
        ids.append(parsed_id)
    if not ids:
        raise HistoryFieldCleanupError(
            "At least one business object id must be specified."
        )
    return ids


def build_cleanup_spec(
    model,
    field_names,
    object_ids,
    replacement_value=None,
    *,
    set_null=False,
    cutoff_history_id=None,
    resume_after_history_id=None,
    using=None,
):
    """Validate a cleanup request and return a frozen spec.

    :param model: The tracked (live) Django model.
    :param field_names: Iterable of field names on the history model that
        should be rewritten.
    :param object_ids: Iterable of primary keys of the live objects whose
        history may be touched.
    :param replacement_value: Value to write into every selected field.
        Required unless ``set_null`` is true.
    :param set_null: When true, set the fields to ``NULL`` instead.
    :param cutoff_history_id: Fixed upper boundary (inclusive) on
        ``history_id``. When omitted, the boundary is captured once from
        the in-scope rows at the start of the run, so history created
        concurrently is never chased.
    :param resume_after_history_id: Lower boundary (exclusive) on
        ``history_id``, used to resume an interrupted run. Pass the
        ``last_history_id`` reported by the previous run.
    :param using: Database alias to use.
    """
    try:
        history_model = get_history_model_for_model(model)
    except NotHistoricalModelError as error:
        raise HistoryFieldCleanupError(str(error))

    fields = _cleaned_fields(history_model, field_names)
    if not fields:
        raise HistoryFieldCleanupError("At least one field must be specified.")

    for field in fields:
        _validate_target_field(field)
        _validate_nullable(field, set_null)

    ids = _parse_object_ids(model, object_ids)

    coerced_value = None
    if not set_null:
        if replacement_value is None:
            raise HistoryFieldCleanupError(
                "A replacement value is required (or use set_null=True)."
            )
        coerced_value = _coerce_replacement_value(fields[0], replacement_value)
        for field in fields[1:]:
            candidate = _coerce_replacement_value(field, replacement_value)
            if candidate != coerced_value or type(candidate) is not type(coerced_value):
                raise HistoryFieldCleanupError(
                    "A single replacement value cannot be used for fields of "
                    "different types; clean them in separate runs."
                )

    coerced_cutoff = cutoff_history_id
    if coerced_cutoff is not None:
        coerced_cutoff = _coerce_history_id(history_model, cutoff_history_id)
    coerced_resume_after = resume_after_history_id
    if coerced_resume_after is not None:
        coerced_resume_after = _coerce_history_id(
            history_model, resume_after_history_id
        )

    return HistoryFieldCleanupSpec(
        model=model,
        history_model=history_model,
        fields=tuple(fields),
        object_ids=tuple(ids),
        replacement_value=coerced_value,
        set_null=set_null,
        cutoff_history_id=coerced_cutoff,
        resume_after_history_id=coerced_resume_after,
    )


def _coerce_history_id(history_model, raw_value):
    try:
        return history_model._meta.pk.to_python(raw_value)
    except (TypeError, ValueError, ValidationError) as error:
        raise HistoryFieldCleanupError(
            f"Invalid cutoff history id '{raw_value}': {error}"
        )


def _base_queryset(spec, using=None):
    pk_attname = spec.model._meta.pk.attname
    qs = spec.history_model.objects.using(using).filter(
        **{f"{pk_attname}__in": list(spec.object_ids)}
    )
    if spec.cutoff_history_id is not None:
        qs = qs.filter(pk__lte=spec.cutoff_history_id)
    if spec.resume_after_history_id is not None:
        qs = qs.filter(pk__gt=spec.resume_after_history_id)
    return qs


def _needs_change_q(spec):
    """Q matching rows where at least one selected field still differs."""
    value = None if spec.set_null else spec.replacement_value
    condition = Q()
    for field in spec.fields:
        if value is None:
            condition |= Q(**{f"{field.attname}__isnull": False})
        else:
            condition |= Q(**{f"{field.attname}__isnull": True})
            condition |= ~Q(**{field.attname: value})
    return condition


def clean_history_fields(
    spec,
    *,
    batch_size=500,
    dry_run=True,
    using=None,
    on_batch=None,
):
    """Run (or preview) the cleanup described by ``spec``.

    The upper ``history_id`` boundary is fixed before any write, either from
    the spec or by snapshotting the highest in-scope ``history_id`` once, so
    history rows inserted concurrently are never followed.

    :param spec: A :class:`HistoryFieldCleanupSpec` from
        :func:`build_cleanup_spec`.
    :param batch_size: Number of history rows to update per batch.
    :param dry_run: When true (the default) only count what would change;
        no writes are issued.
    :param using: Database alias.
    :param on_batch: Optional callable receiving a
        :class:`HistoryFieldCleanupBatch` after each batch. Runs that are
        interrupted can be resumed with a new spec carrying the same
        ``cutoff_history_id`` and the last reported ``last_history_id`` as
        ``resume_after_history_id``.
    """
    if batch_size < 1:
        raise HistoryFieldCleanupError("batch_size must be a positive integer.")

    qs = _base_queryset(spec, using=using)

    cutoff = spec.cutoff_history_id
    if cutoff is None:
        cutoff = qs.order_by("-history_id").values_list("history_id", flat=True).first()

    result = HistoryFieldCleanupResult(cutoff_history_id=cutoff)
    if cutoff is None:
        return result

    qs = qs.filter(pk__lte=cutoff)
    update_values = {
        field.attname: (None if spec.set_null else spec.replacement_value)
        for field in spec.fields
    }
    needs_change = _needs_change_q(spec)

    if dry_run:
        result.rows_in_scope = qs.count()
        result.rows_changed = qs.filter(needs_change).count()
        return result

    while True:
        with transaction.atomic(using=using):
            id_batch = list(
                qs.values_list("history_id", flat=True)
                .order_by("history_id")[:batch_size]
            )
            if not id_batch:
                break
            batch_qs = spec.history_model.objects.using(using).filter(
                history_id__in=id_batch
            )
            in_scope = len(id_batch)
            changed = batch_qs.filter(needs_change).update(**update_values)
            last_id = id_batch[-1]

        result.batches += 1
        result.rows_in_scope += in_scope
        result.rows_changed += changed
        result.last_history_id = last_id
        if on_batch is not None:
            on_batch(
                HistoryFieldCleanupBatch(
                    batch_number=result.batches,
                    rows_in_scope=in_scope,
                    rows_changed=changed,
                    last_history_id=last_id,
                )
            )
        qs = qs.filter(history_id__gt=last_id)
        if len(id_batch) < batch_size:
            break

    return result
