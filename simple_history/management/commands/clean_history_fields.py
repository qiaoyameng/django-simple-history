from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from simple_history.cleanup import (
    HistoryFieldCleanupError,
    build_cleanup_spec,
    clean_history_fields,
)
from simple_history.exceptions import NotHistoricalModelError
from simple_history.utils import get_history_model_for_model


def _split_csv(values):
    result = []
    for value in values:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return result


class Command(BaseCommand):
    args = "<app.model>"
    help = (
        "Rewrite tracked fields (e.g. email, phone) in historical records to "
        "a fixed replacement value without touching audit identity fields "
        "(history_id, history_date, history_user, ...) or the live table."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "model",
            type=str,
            help="Target model as 'app_label.model_name' (exactly one).",
        )
        parser.add_argument(
            "--fields",
            nargs="+",
            required=True,
            help=(
                "Field name(s) on the history model. Accepts space or comma "
                "separated values."
            ),
        )
        parser.add_argument(
            "--pks",
            nargs="+",
            required=True,
            help=(
                "Primary key(s) of the business objects whose history is "
                "scrubbed. Accepts space or comma separated values."
            ),
        )
        replacement = parser.add_mutually_exclusive_group(required=True)
        replacement.add_argument(
            "--replace",
            type=str,
            help="Replacement value written into every selected field.",
        )
        replacement.add_argument(
            "--set-null",
            action="store_true",
            help="Set selected fields to NULL (fields must be nullable).",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "-d",
            "--dry-run",
            action="store_true",
            help="Preview only: report counts, make no changes (default).",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Actually perform the rewrite.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="History rows updated per batch (default: 500).",
        )
        parser.add_argument(
            "--cutoff-history-id",
            type=str,
            default=None,
            help=(
                "Fixed inclusive upper boundary on history_id. Defaults to "
                "the highest in-scope history_id captured when the run starts."
            ),
        )
        parser.add_argument(
            "--resume-after-history-id",
            type=str,
            default=None,
            help=(
                "Exclusive lower boundary for resuming an interrupted run; "
                "use the last_history_id reported previously."
            ),
        )

    def handle(self, *args, **options):
        self.verbosity = options["verbosity"]
        model = self._resolve_model(options["model"])
        try:
            spec = build_cleanup_spec(
                model,
                field_names=_split_csv(options["fields"]),
                object_ids=_split_csv(options["pks"]),
                replacement_value=options["replace"],
                set_null=options["set_null"],
                cutoff_history_id=options["cutoff_history_id"],
                resume_after_history_id=options["resume_after_history_id"],
            )
        except HistoryFieldCleanupError as error:
            raise CommandError(str(error))

        dry_run = not options["apply"]
        field_names = ", ".join(field.name for field in spec.fields)
        self.log(
            f"Model: {model._meta.label} -> {spec.history_model._meta.label}"
        )
        self.log(f"Fields: {field_names}")
        self.log(f"Objects: {len(spec.object_ids)}")
        self.log(f"Cutoff history_id (inclusive): {spec.cutoff_history_id or '<auto>'}")
        if spec.resume_after_history_id is not None:
            self.log(
                "Resuming after history_id: "
                f"{spec.resume_after_history_id} (exclusive)"
            )
        self.log("Mode: DRY RUN (no changes)" if dry_run else "Mode: APPLY")

        def report(batch):
            self.log(
                f"Batch {batch.batch_number}: {batch.rows_changed}/"
                f"{batch.rows_in_scope} rows changed; "
                f"last_history_id={batch.last_history_id}"
            )

        try:
            result = clean_history_fields(
                spec,
                batch_size=options["batch_size"],
                dry_run=dry_run,
                on_batch=report,
            )
        except HistoryFieldCleanupError as error:
            raise CommandError(str(error))

        changed_label = "that would be changed" if dry_run else "changed"
        self.log(
            f"Rows in scope: {result.rows_in_scope}; rows {changed_label}: "
            f"{result.rows_changed}; batches: {result.batches}"
        )
        self.log(f"Fixed cutoff history_id: {result.cutoff_history_id}")
        if result.last_history_id is not None:
            self.log(
                "Resume token (if interrupted, rerun with the same "
                f"--cutoff-history-id and --resume-after-history-id="
                f"{result.last_history_id}):"
            )
            self.log(
                f"  --cutoff-history-id={result.cutoff_history_id} "
                f"--resume-after-history-id={result.last_history_id}"
            )
        if dry_run:
            self.log("Dry run complete. Re-run with --apply to perform the rewrite.")

    def _resolve_model(self, natural_key):
        try:
            app_label, model_name = natural_key.split(".", 1)
        except ValueError:
            raise CommandError(
                f"Invalid model '{natural_key}'; expected 'app_label.model_name'."
            )
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            raise CommandError(f"Unable to find model < {natural_key} >.")
        try:
            get_history_model_for_model(model)
        except NotHistoricalModelError:
            raise CommandError(f"No history model found < {natural_key} >.")
        return model

    def log(self, message, verbosity_level=1):
        if self.verbosity >= verbosity_level:
            self.stdout.write(message)
