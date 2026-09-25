"""Scrub sensitive field values out of historical records.

Unlike ``populate_history``/``clean_old_history``/``clean_duplicate_history``
this command never touches the live business table and never deletes audit
information: only the explicitly selected data columns of the explicitly
selected objects' historical rows are rewritten.
"""
from django.apps import apps
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from ...exceptions import NotHistoricalModelError
from ...utils import get_history_model_for_model

# Audit identity columns. These identify *when* and *by whom* a historical
# change happened and must never be rewritten by a data scrub.
PROTECTED_HISTORY_FIELDS = frozenset(
    {
        "history_id",
        "history_date",
        "history_type",
        "history_user_id",
        "history_change_reason",
        "history_relation_id",
        "m2m_history_id",
    }
)


class Command(BaseCommand):
    help = (
        "Replace sensitive field values (e.g. email/phone) in the historical "
        "records of explicitly selected objects. The live business table is "
        "never modified and audit fields (history_id, history_date, "
        "history_user, ...) are never touched."
    )

    MODEL_NOT_FOUND = "Unable to find model"
    MODEL_NOT_HISTORICAL = "No history model found"
    M2M_HISTORY_UNSUPPORTED = (
        "is an m2m history model; field cleanup is only supported for regular "
        "historical models"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "model",
            type=str,
            help="Target business model as 'app_label.model_name'.",
        )
        parser.add_argument(
            "--pks",
            required=True,
            type=str,
            help=(
                "Comma-separated primary keys of the business objects whose "
                "history should be cleaned (e.g. '1,4,7')."
            ),
        )
        parser.add_argument(
            "--fields",
            required=True,
            nargs="+",
            help="Model field names to replace in the historical records.",
        )
        replacement = parser.add_mutually_exclusive_group(required=True)
        replacement.add_argument(
            "--value",
            type=str,
            help="Single replacement value applied to every selected field.",
        )
        replacement.add_argument(
            "--values",
            nargs="+",
            help=(
                "Per-field replacements as field=value pairs "
                "(e.g. email=redacted@example.com phone=000)."
            ),
        )
        replacement.add_argument(
            "--null",
            action="store_true",
            help="Replace selected fields with NULL (fields must be nullable).",
        )
        parser.add_argument(
            "-d",
            "--dry-run",
            action="store_true",
            help="Only report how many historical rows would change; write nothing.",
        )
        parser.add_argument(
            "--batchsize",
            type=int,
            default=200,
            help="Number of historical rows processed per batch (default: 200).",
        )
        parser.add_argument(
            "--cutoff",
            type=str,
            default=None,
            help=(
                "ISO-8601 datetime boundary. Only history rows with "
                "history_date <= cutoff are considered. Defaults to the time "
                "the command starts, so concurrent inserts are never chased."
            ),
        )
        parser.add_argument(
            "--resume-after-history-id",
            type=str,
            default=None,
            help=(
                "Resume a previous interrupted run: rows with history_id up to "
                "and including this value are skipped. Reuse the same --cutoff "
                "as the original run."
            ),
        )
        parser.add_argument(
            "--database",
            default="default",
            help="Database alias to operate on (default: 'default').",
        )

    def handle(self, *args, **options):
        self.verbosity = options["verbosity"]
        database = options["database"]

        model, history_model = self._resolve_model(options["model"])
        history_attnames = {field.attname for field in history_model._meta.fields}
        # m2m historical models have a different shape (no copied business
        # columns); rewriting their columns is intentionally not supported.
        if "m2m_history_id" in history_attnames:
            raise CommandError(
                f"{history_model.__name__} {self.M2M_HISTORY_UNSUPPORTED}"
            )

        pks = self._parse_pks(model, options["pks"])
        replacements = self._build_replacements(model, history_model, options)
        cutoff = self._parse_cutoff(options["cutoff"])
        resume_after_id = self._parse_resume_cursor(
            history_model, options["resume_after_history_id"]
        )

        summary = self._process(
            model=model,
            history_model=history_model,
            pks=pks,
            replacements=replacements,
            cutoff=cutoff,
            resume_after_id=resume_after_id,
            batch_size=options["batchsize"],
            dry_run=options["dry_run"],
            database=database,
        )
        self._report(summary)

    def _resolve_model(self, natural_key):
        try:
            app_label, model_name = natural_key.split(".", 1)
        except ValueError:
            raise CommandError(f"{self.MODEL_NOT_FOUND} < {natural_key} >")
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            raise CommandError(f"{self.MODEL_NOT_FOUND} < {natural_key} >")
        try:
            history_model = get_history_model_for_model(model)
        except NotHistoricalModelError:
            raise CommandError(f"{self.MODEL_NOT_HISTORICAL} < {natural_key} >")
        return model, history_model

    def _parse_pks(self, model, raw_pks):
        pk_field = model._meta.pk
        pks = []
        invalid = []
        for raw in raw_pks.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                pks.append(pk_field.to_python(raw))
            except ValidationError:
                invalid.append(raw)
        if invalid or not pks:
            raise CommandError(
                "Invalid --pks for {} primary key {!r}: {}".format(
                    model._meta.label, pk_field.__class__.__name__, invalid or raw_pks
                )
            )
        # De-duplicate while preserving order.
        return list(dict.fromkeys(pks))

    def _build_replacements(self, model, history_model, options):
        """Validate selected fields and return {attname: cleaned value}."""
        if options["null"]:
            raw_values = {name: None for name in options["fields"]}
        elif options["value"] is not None:
            raw_values = {name: options["value"] for name in options["fields"]}
        else:
            raw_values = {}
            for pair in options["values"]:
                if "=" not in pair:
                    raise CommandError(
                        f"--values entries must be field=value pairs (got {pair!r})"
                    )
                field_name, value = pair.split("=", 1)
                raw_values[field_name.strip()] = value
            missing = [name for name in options["fields"] if name not in raw_values]
            if missing or set(raw_values) != set(options["fields"]):
                raise CommandError(
                    "--values must provide exactly one value for each --fields "
                    f"entry (fields={options['fields']})"
                )

        history_attnames = {field.attname for field in history_model._meta.fields}
        pk_attname = model._meta.pk.attname
        # Map both column names (attname) and declared names onto model fields.
        concrete_fields = {}
        for candidate in model._meta.get_fields():
            if getattr(candidate, "column", None) is None:
                continue
            concrete_fields[candidate.name] = candidate
            concrete_fields[candidate.attname] = candidate
        replacements = {}
        for field_name, raw_value in raw_values.items():
            if field_name in PROTECTED_HISTORY_FIELDS:
                raise CommandError(
                    f"Field {field_name!r} is an audit identity field and must "
                    "not be modified."
                )
            field = concrete_fields.get(field_name)
            if field is None:
                try:
                    field = model._meta.get_field(field_name)
                except FieldDoesNotExist:
                    field = None
            if field is None or not field.concrete:
                raise CommandError(
                    f"Model {model._meta.label} has no concrete field named "
                    f"{field_name!r}."
                )
            if field.is_relation:
                raise CommandError(
                    f"Field {field_name!r} is a relation field; only plain "
                    "(scalar) model columns can be cleaned."
                )
            if field.attname == pk_attname:
                raise CommandError(
                    f"Field {field_name!r} is the primary key and must not be "
                    "modified."
                )
            if field.attname not in history_attnames:
                raise CommandError(
                    f"Field {field_name!r} is not stored on the history model "
                    f"{history_model._meta.label}."
                )
            if raw_value is None:
                if not field.null:
                    raise CommandError(
                        f"Field {field_name!r} is NOT NULL and cannot be replaced "
                        "with NULL; supply --value/--values instead."
                    )
                value = None
            else:
                try:
                    value = field.to_python(raw_value)
                    # Run validators (e.g. EmailField, max_length semantics).
                    field.clean(value, model_instance=None)
                except ValidationError as error:
                    messages = [
                        child.messages[0]
                        if hasattr(child, "messages") and child.messages
                        else str(child)
                        for child in error.error_list
                    ] or error.messages
                    raise CommandError(
                        f"Replacement value for {field_name!r} is invalid: "
                        + "; ".join(messages)
                    )
            replacements[field.attname] = value
        return replacements

    def _parse_cutoff(self, raw_cutoff):
        if raw_cutoff is None:
            from django.utils import timezone

            return timezone.now()
        cutoff = parse_datetime(raw_cutoff)
        if cutoff is None:
            raise CommandError(
                f"--cutoff must be an ISO-8601 datetime (got {raw_cutoff!r})."
            )
        from django.conf import settings
        from django.utils import timezone

        if settings.USE_TZ and timezone.is_naive(cutoff):
            cutoff = timezone.make_aware(cutoff)
        return cutoff

    def _parse_resume_cursor(self, history_model, raw_cursor):
        if raw_cursor is None:
            return None
        history_pk = history_model._meta.pk
        try:
            return history_pk.to_python(raw_cursor)
        except ValidationError:
            raise CommandError(
                f"Invalid --resume-after-history-id for "
                f"{history_model._meta.pk.__class__.__name__}: {raw_cursor!r}"
            )

    def _base_queryset(
        self, history_model, pk_attname, pks, cutoff, resume_after_id, database
    ):
        queryset = history_model.objects.using(database).filter(
            **{f"{pk_attname}__in": pks},
            history_date__lte=cutoff,
        )
        if resume_after_id is not None:
            queryset = queryset.filter(history_id__gt=resume_after_id)
        return queryset.order_by("history_id")

    def _process(
        self,
        model,
        history_model,
        pks,
        replacements,
        cutoff,
        resume_after_id,
        batch_size,
        dry_run,
        database,
    ):
        if batch_size < 1:
            raise CommandError("--batchsize must be a positive integer.")

        pk_attname = model._meta.pk.attname
        base_queryset = self._base_queryset(
            history_model, pk_attname, pks, cutoff, resume_after_id, database
        )
        scanned = 0
        matched = 0
        batches = 0
        last_history_id = resume_after_id

        while True:
            batch_queryset = base_queryset
            if last_history_id is not None:
                batch_queryset = batch_queryset.filter(history_id__gt=last_history_id)
            rows = list(batch_queryset[:batch_size])
            if not rows:
                break
            batches += 1

            update_ids = []
            for row in rows:
                scanned += 1
                needs_update = False
                for attname, replacement in replacements.items():
                    if getattr(row, attname) != replacement:
                        needs_update = True
                        break
                if needs_update:
                    update_ids.append(row.history_id)

            if update_ids and not dry_run:
                with transaction.atomic(using=database):
                    history_model.objects.using(database).filter(
                        history_id__in=update_ids
                    ).update(**replacements)

            matched += len(update_ids)
            last_history_id = rows[-1].history_id
            self.log(
                "{}batch {}: {} rows scanned, {} would change, "
                "resume-after-history-id={}".format(
                    "[dry-run] " if dry_run else "",
                    batches,
                    len(rows),
                    len(update_ids),
                    last_history_id,
                ),
                verbosity_level=2,
            )

        return {
            "model": model._meta.label,
            "history_model": history_model._meta.label,
            "fields": sorted(replacements),
            "dry_run": dry_run,
            "cutoff": cutoff.isoformat(),
            "objects": len(pks),
            "scanned": scanned,
            "to_change": matched,
            "changed": 0 if dry_run else matched,
            "batches": batches,
            "resume_after_history_id": last_history_id,
        }

    def _report(self, summary):
        mode = "dry-run" if summary["dry_run"] else "apply"
        changed_key = "to_change" if summary["dry_run"] else "changed"
        self.log(
            f"clean_history_fields [{mode}] model={summary['model']} "
            f"history={summary['history_model']} "
            f"fields={','.join(summary['fields'])} "
            f"objects={summary['objects']} cutoff={summary['cutoff']} "
            f"scanned={summary['scanned']} {changed_key}={summary['to_change']} "
            f"batches={summary['batches']} "
            f"resume_after_history_id={summary['resume_after_history_id']}"
        )

    def log(self, message, verbosity_level=1):
        if self.verbosity >= verbosity_level:
            self.stdout.write(message)
