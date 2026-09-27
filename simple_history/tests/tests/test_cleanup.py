from io import StringIO

from django.core import management
from django.core.management.base import CommandError
from django.test import TestCase

from simple_history.cleanup import (
    HistoryFieldCleanupError,
    build_cleanup_spec,
    clean_history_fields,
)
from simple_history.tests.models import Contact, Series, SeriesWork, UUIDModel


class HistoryFieldCleanupTests(TestCase):
    def setUp(self):
        self.contact = Contact.objects.create(
            name="Ada", email="ada@example.com", phone="+1-555-0100"
        )
        self.contact.email = "ada.new@example.com"
        self.contact.phone = "+1-555-0101"
        self.contact.save()
        self.other = Contact.objects.create(
            name="Grace", email="grace@example.com", phone="+1-555-0200"
        )

    def history_rows(self, contact):
        return list(
            Contact.history.filter(id=contact.pk)
            .order_by("history_id")
            .values("history_id", "email", "phone", "name", "history_date")
        )

    def test_dry_run_reports_counts_without_writing(self):
        spec = build_cleanup_spec(
            Contact, ["email"], [self.contact.pk], "redacted@example.invalid"
        )
        result = clean_history_fields(spec, dry_run=True)
        self.assertEqual(result.rows_in_scope, 2)
        self.assertEqual(result.rows_changed, 2)
        self.assertIsNotNone(result.cutoff_history_id)
        self.assertEqual(self.history_rows(self.contact)[0]["email"], "ada@example.com")

    def test_apply_replaces_only_target_history_fields(self):
        before = self.history_rows(self.contact)
        other_before = self.history_rows(self.other)
        spec = build_cleanup_spec(
            Contact, ["email"], [self.contact.pk], "redacted@example.invalid"
        )
        result = clean_history_fields(spec, dry_run=False)
        self.assertEqual(result.rows_changed, 2)

        after = self.history_rows(self.contact)
        self.assertEqual(
            [row["email"] for row in after],
            ["redacted@example.invalid"] * 2,
        )
        for old, new in zip(before, after):
            self.assertEqual(old["history_id"], new["history_id"])
            self.assertEqual(old["history_date"], new["history_date"])
            self.assertEqual(old["name"], new["name"])
            self.assertEqual(old["phone"], new["phone"])
        self.assertEqual(self.history_rows(self.other), other_before)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.email, "ada.new@example.com")

    def test_set_null_requires_nullable_field(self):
        with self.assertRaises(HistoryFieldCleanupError):
            build_cleanup_spec(
                Contact, ["email"], [self.contact.pk], set_null=True
            )

    def test_set_null_on_nullable_field(self):
        spec = build_cleanup_spec(
            Contact, ["phone"], [self.contact.pk], set_null=True
        )
        result = clean_history_fields(spec, dry_run=False)
        self.assertEqual(result.rows_changed, 2)
        self.assertEqual(
            list(
                Contact.history.filter(id=self.contact.pk).values_list(
                    "phone", flat=True
                )
            ),
            [None, None],
        )

    def test_repeated_run_is_idempotent(self):
        spec = build_cleanup_spec(
            Contact, ["email"], [self.contact.pk], "redacted@example.invalid"
        )
        first = clean_history_fields(spec, dry_run=False)
        snapshot = list(
            Contact.history.filter(id=self.contact.pk).values(
                "history_id", "email", "history_date"
            )
        )
        second = clean_history_fields(spec, dry_run=False)
        self.assertEqual(second.rows_changed, 0)
        self.assertEqual(
            list(
                Contact.history.filter(id=self.contact.pk).values(
                    "history_id", "email", "history_date"
                )
            ),
            snapshot,
        )
        self.assertEqual(first.cutoff_history_id, second.cutoff_history_id)

    def test_concurrently_inserted_history_beyond_cutoff_is_untouched(self):
        spec = build_cleanup_spec(
            Contact, ["email"], [self.contact.pk], "redacted@example.invalid"
        )
        result = clean_history_fields(spec, dry_run=True)
        cutoff = result.cutoff_history_id

        self.contact.email = "after.snapshot@example.com"
        self.contact.save()

        rerun = build_cleanup_spec(
            Contact,
            ["email"],
            [self.contact.pk],
            "redacted@example.invalid",
            cutoff_history_id=cutoff,
        )
        applied = clean_history_fields(rerun, dry_run=False)
        self.assertEqual(applied.rows_in_scope, 2)
        untouched = Contact.history.filter(id=self.contact.pk).exclude(
            history_id__lte=cutoff
        ).get()
        self.assertEqual(untouched.email, "after.snapshot@example.com")

    def test_resume_with_cutoff_completes_remaining_rows(self):
        spec = build_cleanup_spec(
            Contact, ["email"], [self.contact.pk], "redacted@example.invalid"
        )
        preview = clean_history_fields(spec, dry_run=True)
        cutoff = preview.cutoff_history_id
        first_id = (
            Contact.history.filter(id=self.contact.pk)
            .order_by("history_id")
            .values_list("history_id", flat=True)
            .first()
        )

        first_spec = build_cleanup_spec(
            Contact,
            ["email"],
            [self.contact.pk],
            "redacted@example.invalid",
            cutoff_history_id=cutoff,
        )

        class Interrupted(Exception):
            pass

        def stop_after_first(batch):
            raise Interrupted

        with self.assertRaises(Interrupted):
            clean_history_fields(
                first_spec,
                batch_size=1,
                dry_run=False,
                on_batch=stop_after_first,
            )
        first_result_email = Contact.history.get(history_id=first_id).email
        self.assertEqual(first_result_email, "redacted@example.invalid")
        self.assertEqual(
            Contact.history.filter(id=self.contact.pk)
            .exclude(history_id=first_id)
            .get()
            .email,
            "ada.new@example.com",
        )

        resume_spec = build_cleanup_spec(
            Contact,
            ["email"],
            [self.contact.pk],
            "redacted@example.invalid",
            cutoff_history_id=cutoff,
            resume_after_history_id=first_id,
        )
        resume_result = clean_history_fields(resume_spec, batch_size=1, dry_run=False)
        self.assertEqual(resume_result.rows_changed, 1)
        self.assertFalse(
            Contact.history.filter(id=self.contact.pk)
            .exclude(email="redacted@example.invalid")
            .exists()
        )

    def test_batch_callbacks_report_progress(self):
        spec = build_cleanup_spec(
            Contact, ["email"], [self.contact.pk], "redacted@example.invalid"
        )
        batches = []
        clean_history_fields(
            spec, batch_size=1, dry_run=False, on_batch=batches.append
        )
        self.assertEqual(len(batches), 2)
        self.assertEqual([b.batch_number for b in batches], [1, 2])
        self.assertTrue(
            batches[0].last_history_id < batches[1].last_history_id
        )

    def test_audit_fields_are_rejected(self):
        for field_name in ("history_id", "history_date", "history_user_id",
                           "history_change_reason", "history_type",
                           "history_relation"):
            with self.subTest(field=field_name):
                with self.assertRaises(HistoryFieldCleanupError):
                    build_cleanup_spec(
                        Contact,
                        [field_name],
                        [self.contact.pk],
                        "x",
                    )

    def test_relation_fields_are_rejected(self):
        series = Series.objects.create(name="trilogy", author="someone")
        work = SeriesWork.objects.create(series=series, title="work 1")
        with self.assertRaises(HistoryFieldCleanupError):
            build_cleanup_spec(SeriesWork, ["series_id"], [work.pk], 1)

    def test_invalid_replacement_value_is_rejected_before_writes(self):
        with self.assertRaises(HistoryFieldCleanupError):
            build_cleanup_spec(
                Contact,
                ["email"],
                [self.contact.pk],
                "x" * 300,
            )
        self.assertEqual(
            Contact.history.filter(id=self.contact.pk).count(), 2
        )

    def test_empty_request_is_rejected(self):
        with self.assertRaises(HistoryFieldCleanupError):
            build_cleanup_spec(Contact, ["email"], [], "redacted@example.invalid")

    def test_uuid_history_id_boundary(self):
        obj = UUIDModel.objects.create(name="ada@example.com")
        preview = clean_history_fields(
            build_cleanup_spec(
                UUIDModel,
                ["name"],
                [obj.pk],
                "redacted@example.invalid",
            ),
            dry_run=True,
        )
        self.assertEqual(preview.rows_in_scope, 1)
        self.assertEqual(preview.rows_changed, 1)
        applied = clean_history_fields(
            build_cleanup_spec(
                UUIDModel,
                ["name"],
                [obj.pk],
                "redacted@example.invalid",
                cutoff_history_id=str(preview.cutoff_history_id),
            ),
            dry_run=False,
        )
        self.assertEqual(applied.rows_changed, 1)
        self.assertEqual(UUIDModel.history.get().name, "redacted@example.invalid")


class CleanHistoryFieldsCommandTests(TestCase):
    command_name = "clean_history_fields"

    def setUp(self):
        self.contact = Contact.objects.create(
            name="Ada", email="ada@example.com", phone="+1-555-0100"
        )

    def call(self, *args, **kwargs):
        out = StringIO()
        err = StringIO()
        management.call_command(
            self.command_name,
            *args,
            stdout=out,
            stderr=err,
            **kwargs,
        )
        return out.getvalue(), err.getvalue()

    def test_dry_run_is_default_and_does_not_modify(self):
        output, _ = self.call(
            "tests.contact",
            fields=["email"],
            pks=[str(self.contact.pk)],
            replace="redacted@example.invalid",
        )
        self.assertIn("DRY RUN", output)
        self.assertIn("rows that would be changed: 1", output)
        self.assertEqual(
            Contact.history.filter(id=self.contact.pk).get().email,
            "ada@example.com",
        )

    def test_apply_mode_rewrites_history(self):
        output, _ = self.call(
            "tests.contact",
            fields=["email"],
            pks=[str(self.contact.pk)],
            replace="redacted@example.invalid",
            apply=True,
        )
        self.assertIn("Mode: APPLY", output)
        self.assertNotIn("ada@example.com", output)
        self.assertEqual(
            Contact.history.filter(id=self.contact.pk).get().email,
            "redacted@example.invalid",
        )

    def test_audit_field_rejected(self):
        with self.assertRaises(CommandError):
            self.call(
                "tests.contact",
                fields=["history_date"],
                pks=[str(self.contact.pk)],
                replace="x",
            )

    def test_non_historical_model_rejected(self):
        with self.assertRaises(CommandError):
            self.call(
                "auth.permission",
                fields=["name"],
                pks=["1"],
                replace="x",
            )

    def test_unknown_model_rejected(self):
        with self.assertRaises(CommandError):
            self.call(
                "tests.nope",
                fields=["email"],
                pks=["1"],
                replace="x",
            )

    def test_comma_separated_arguments(self):
        Contact.objects.create(name="B", email="b@example.com")
        output, _ = self.call(
            "tests.contact",
            fields=["email"],
            pks=[f"{self.contact.pk},999999"],
            replace="redacted@example.invalid",
        )
        self.assertIn("rows that would be changed: 1", output)

    def test_set_null_flag(self):
        self.call(
            "tests.contact",
            fields=["phone"],
            pks=[str(self.contact.pk)],
            set_null=True,
            apply=True,
        )
        self.assertIsNone(Contact.history.filter(id=self.contact.pk).get().phone)
