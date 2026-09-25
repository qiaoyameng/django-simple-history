from contextlib import contextmanager
from datetime import datetime, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import management
from django.test import TestCase

from simple_history import models as sh_models
from simple_history.management.commands import (
    clean_duplicate_history,
    clean_old_history,
    populate_history,
)

from ..models import (
    Book,
    Choice,
    Contact,
    CustomManagerNameModel,
    Place,
    Poll,
    PollWithCustomManager,
    PollWithExcludeFields,
    Restaurant,
)


@contextmanager
def replace_registry(new_value=None):
    hidden_registry = sh_models.registered_models
    sh_models.registered_models = new_value or {}
    try:
        yield
    except Exception:
        raise
    finally:
        sh_models.registered_models = hidden_registry


class TestPopulateHistory(TestCase):
    command_name = "populate_history"
    command_error = (management.CommandError, SystemExit)

    def test_no_args(self):
        out = StringIO()
        management.call_command(self.command_name, stdout=out, stderr=StringIO())
        self.assertIn(populate_history.Command.COMMAND_HINT, out.getvalue())

    def test_bad_args(self):
        test_data = (
            (populate_history.Command.MODEL_NOT_HISTORICAL, ("tests.place",)),
            (populate_history.Command.MODEL_NOT_FOUND, ("invalid.model",)),
            (populate_history.Command.MODEL_NOT_FOUND, ("bad_key",)),
        )
        for msg, args in test_data:
            out = StringIO()
            self.assertRaises(
                self.command_error,
                management.call_command,
                self.command_name,
                *args,
                stdout=StringIO(),
                stderr=out,
            )
            self.assertIn(msg, out.getvalue())

    def test_auto_populate(self):
        Poll.objects.create(question="Will this populate?", pub_date=datetime.now())
        Poll.history.all().delete()
        management.call_command(
            self.command_name, auto=True, stdout=StringIO(), stderr=StringIO()
        )
        self.assertEqual(Poll.history.all().count(), 1)

    def test_populate_with_custom_batch_size(self):
        Poll.objects.create(question="Will this populate?", pub_date=datetime.now())
        Poll.history.all().delete()
        management.call_command(
            self.command_name,
            auto=True,
            batchsize=500,
            stdout=StringIO(),
            stderr=StringIO(),
        )
        self.assertEqual(Poll.history.all().count(), 1)

    def test_specific_populate(self):
        Poll.objects.create(question="Will this populate?", pub_date=datetime.now())
        Poll.history.all().delete()
        Book.objects.create(isbn="9780007117116")
        Book.history.all().delete()
        management.call_command(
            self.command_name, "tests.book", stdout=StringIO(), stderr=StringIO()
        )
        self.assertEqual(Book.history.all().count(), 1)
        self.assertEqual(Poll.history.all().count(), 0)

    def test_failing_wont_save(self):
        Poll.objects.create(question="Will this populate?", pub_date=datetime.now())
        Poll.history.all().delete()
        self.assertRaises(
            self.command_error,
            management.call_command,
            self.command_name,
            "tests.poll",
            "tests.invalid_model",
            stdout=StringIO(),
            stderr=StringIO(),
        )
        self.assertEqual(Poll.history.all().count(), 0)

    def test_multi_table(self):
        data = {"rating": 5, "name": "Tea 'N More"}
        Restaurant.objects.create(**data)
        Restaurant.updates.all().delete()
        management.call_command(
            self.command_name, "tests.restaurant", stdout=StringIO(), stderr=StringIO()
        )
        update_record = Restaurant.updates.all()[0]
        for attr, value in data.items():
            self.assertEqual(getattr(update_record, attr), value)

    def test_existing_objects(self):
        data = {"rating": 5, "name": "Tea 'N More"}
        out = StringIO()
        Restaurant.objects.create(**data)
        pre_call_count = Restaurant.updates.count()
        management.call_command(
            self.command_name, "tests.restaurant", stdout=StringIO(), stderr=out
        )
        self.assertEqual(Restaurant.updates.count(), pre_call_count)
        self.assertIn(populate_history.Command.EXISTING_HISTORY_FOUND, out.getvalue())

    def test_no_historical(self):
        out = StringIO()
        with replace_registry({"test_place": Place}):
            management.call_command(self.command_name, auto=True, stdout=out)
        self.assertIn(populate_history.Command.NO_REGISTERED_MODELS, out.getvalue())

    def test_batch_processing_with_batch_size_less_than_total(self):
        data = [
            Poll(id=1, question="Question 1", pub_date=datetime.now()),
            Poll(id=2, question="Question 2", pub_date=datetime.now()),
            Poll(id=3, question="Question 3", pub_date=datetime.now()),
            Poll(id=4, question="Question 4", pub_date=datetime.now()),
        ]
        Poll.objects.bulk_create(data)

        management.call_command(
            self.command_name,
            auto=True,
            batchsize=3,
            stdout=StringIO(),
            stderr=StringIO(),
        )

        self.assertEqual(Poll.history.count(), 4)

    def test_stdout_not_printed_when_verbosity_is_0(self):
        out = StringIO()
        Poll.objects.create(question="Question 1", pub_date=datetime.now())

        management.call_command(
            self.command_name,
            auto=True,
            batchsize=3,
            stdout=out,
            stderr=StringIO(),
            verbosity=0,
        )

        self.assertEqual(out.getvalue(), "")

    def test_stdout_printed_when_verbosity_is_not_specified(self):
        out = StringIO()
        Poll.objects.create(question="Question 1", pub_date=datetime.now())

        management.call_command(
            self.command_name, auto=True, batchsize=3, stdout=out, stderr=StringIO()
        )

        self.assertNotEqual(out.getvalue(), "")

    def test_excluded_fields(self):
        poll = PollWithExcludeFields.objects.create(
            question="Will this work?", pub_date=datetime.now()
        )
        PollWithExcludeFields.history.all().delete()
        management.call_command(
            self.command_name,
            "tests.pollwithexcludefields",
            auto=True,
            stdout=StringIO(),
            stderr=StringIO(),
        )
        initial_history_record = PollWithExcludeFields.history.all()[0]
        self.assertEqual(initial_history_record.question, poll.question)


class TestCleanDuplicateHistory(TestCase):
    command_name = "clean_duplicate_history"
    command_error = (management.CommandError, SystemExit)

    def test_no_args(self):
        out = StringIO()
        management.call_command(self.command_name, stdout=out, stderr=StringIO())
        self.assertIn(clean_duplicate_history.Command.COMMAND_HINT, out.getvalue())

    def test_bad_args(self):
        test_data = (
            (clean_duplicate_history.Command.MODEL_NOT_HISTORICAL, ("tests.place",)),
            (clean_duplicate_history.Command.MODEL_NOT_FOUND, ("invalid.model",)),
            (clean_duplicate_history.Command.MODEL_NOT_FOUND, ("bad_key",)),
        )
        for msg, args in test_data:
            out = StringIO()
            self.assertRaises(
                self.command_error,
                management.call_command,
                self.command_name,
                *args,
                stdout=StringIO(),
                stderr=out,
            )
            self.assertIn(msg, out.getvalue())

    def test_no_historical(self):
        out = StringIO()
        with replace_registry({"test_place": Place}):
            management.call_command(self.command_name, auto=True, stdout=out)
        self.assertIn(
            clean_duplicate_history.Command.NO_REGISTERED_MODELS, out.getvalue()
        )

    def test_auto_dry_run(self):
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        p.save()

        # not related to dry_run test, just for increasing coverage :)
        # create instance with single-entry history older than "minutes"
        # so it is skipped
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        h = p.history.first()
        h.history_date -= timedelta(hours=1)
        h.save()

        self.assertEqual(Poll.history.all().count(), 3)
        out = StringIO()
        management.call_command(
            self.command_name,
            auto=True,
            minutes=50,
            dry=True,
            stdout=out,
            stderr=StringIO(),
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.Poll'>\n",
        )
        self.assertEqual(Poll.history.all().count(), 3)

    def test_auto_cleanup(self):
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.save()
        self.assertEqual(Poll.history.all().count(), 2)
        p.question = "Maybe this one won't...?"
        p.save()
        self.assertEqual(Poll.history.all().count(), 3)
        out = StringIO()
        management.call_command(
            self.command_name, auto=True, stdout=out, stderr=StringIO()
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.Poll'>\n",
        )
        self.assertEqual(Poll.history.all().count(), 2)

    def _prepare_cleanup_manager(self):
        one = PollWithCustomManager._default_manager.create(
            question="This is hidden in default manager",
            pub_date=datetime.now(),
            hidden=True,
        )
        one.save()

        two = PollWithCustomManager._default_manager.create(
            question="This is visible in default manager", pub_date=datetime.now()
        )
        two.save()

        self.assertEqual(PollWithCustomManager.history.count(), 4)

    def test_auto_cleanup_defaultmanager(self):
        self._prepare_cleanup_manager()

        out = StringIO()
        management.call_command(
            self.command_name, auto=True, stdout=out, stderr=StringIO()
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.PollWithCustomManager'>\n",
        )
        self.assertEqual(PollWithCustomManager.history.count(), 3)

    def test_auto_cleanup_basemanage(self):
        self._prepare_cleanup_manager()

        out = StringIO()
        management.call_command(
            self.command_name,
            auto=True,
            base_manager=True,
            stdout=out,
            stderr=StringIO(),
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.PollWithCustomManager'>\n"
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.PollWithCustomManager'>\n",
        )
        self.assertEqual(PollWithCustomManager.history.count(), 2)

    def test_auto_cleanup_verbose(self):
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.save()
        p.question = "Maybe this one won't...?"
        p.save()
        self.assertEqual(Poll.history.all().count(), 3)
        out = StringIO()
        management.call_command(
            self.command_name,
            "tests.poll",
            auto=True,
            verbosity=2,
            stdout=out,
            stderr=StringIO(),
        )
        self.assertEqual(
            out.getvalue(),
            "<class 'simple_history.tests.models.Poll'> has 3 historical entries\n"
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.Poll'>\n",
        )
        self.assertEqual(Poll.history.all().count(), 2)

    def test_auto_cleanup_dated(self):
        the_time_is_now = datetime.now()
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=the_time_is_now
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.save()
        p.save()
        self.assertEqual(Poll.history.all().count(), 3)
        p.question = "Or this one...?"
        p.save()
        p.save()
        self.assertEqual(Poll.history.all().count(), 5)

        for h in Poll.history.all()[2:]:
            h.history_date -= timedelta(hours=1)
            h.save()

        management.call_command(
            self.command_name,
            auto=True,
            minutes=50,
            stdout=StringIO(),
            stderr=StringIO(),
        )
        self.assertEqual(Poll.history.all().count(), 4)

    def test_auto_cleanup_dated_extra_one(self):
        the_time_is_now = datetime.now()
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=the_time_is_now
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.save()
        p.save()
        self.assertEqual(Poll.history.all().count(), 3)
        p.question = "Or this one...?"
        p.save()
        p.save()
        p.save()
        p.save()
        self.assertEqual(Poll.history.all().count(), 7)

        for h in Poll.history.all()[2:]:
            h.history_date -= timedelta(hours=1)
            h.save()

        management.call_command(
            self.command_name,
            auto=True,
            minutes=50,
            stdout=StringIO(),
            stderr=StringIO(),
        )
        # even though only the last 2 entries match the date range
        # the "extra_one" (the record before the oldest match)
        # is identical to the oldest match, so oldest match is deleted
        self.assertEqual(Poll.history.all().count(), 5)

    def test_auto_cleanup_custom_history_field(self):
        m = CustomManagerNameModel.objects.create(name="John")
        self.assertEqual(CustomManagerNameModel.log.all().count(), 1)
        m.save()
        self.assertEqual(CustomManagerNameModel.log.all().count(), 2)
        m.name = "Ivan"
        m.save()
        self.assertEqual(CustomManagerNameModel.log.all().count(), 3)
        out = StringIO()
        management.call_command(
            self.command_name, auto=True, stdout=out, stderr=StringIO()
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.CustomManagerNameModel'>\n",
        )
        self.assertEqual(CustomManagerNameModel.log.all().count(), 2)

    def test_auto_cleanup_with_excluded_fields(self):
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.pub_date = p.pub_date + timedelta(days=1)
        p.save()
        self.assertEqual(Poll.history.all().count(), 2)
        out = StringIO()
        management.call_command(
            self.command_name,
            auto=True,
            excluded_fields=("pub_date",),
            stdout=out,
            stderr=StringIO(),
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.Poll'>\n",
        )
        self.assertEqual(Poll.history.all().count(), 1)

    def test_auto_cleanup_for_model_with_excluded_fields(self):
        p = PollWithExcludeFields.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        self.assertEqual(PollWithExcludeFields.history.all().count(), 1)
        p.pub_date = p.pub_date + timedelta(days=1)
        p.save()
        self.assertEqual(PollWithExcludeFields.history.all().count(), 2)
        out = StringIO()
        management.call_command(
            self.command_name, auto=True, stdout=out, stderr=StringIO()
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.PollWithExcludeFields'>\n",
        )
        self.assertEqual(PollWithExcludeFields.history.all().count(), 1)


class TestCleanOldHistory(TestCase):
    command_name = "clean_old_history"
    command_error = (management.CommandError, SystemExit)

    def test_no_args(self):
        out = StringIO()
        management.call_command(self.command_name, stdout=out, stderr=StringIO())
        self.assertIn(clean_old_history.Command.COMMAND_HINT, out.getvalue())

    def test_bad_args(self):
        test_data = (
            (clean_old_history.Command.MODEL_NOT_HISTORICAL, ("tests.place",)),
            (clean_old_history.Command.MODEL_NOT_FOUND, ("invalid.model",)),
            (clean_old_history.Command.MODEL_NOT_FOUND, ("bad_key",)),
        )
        for msg, args in test_data:
            out = StringIO()
            self.assertRaises(
                self.command_error,
                management.call_command,
                self.command_name,
                *args,
                stdout=StringIO(),
                stderr=out,
            )
            self.assertIn(msg, out.getvalue())

    def test_no_historical(self):
        out = StringIO()
        with replace_registry({"test_place": Place}):
            management.call_command(self.command_name, auto=True, stdout=out)
        self.assertIn(clean_old_history.Command.NO_REGISTERED_MODELS, out.getvalue())

    def test_auto_dry_run(self):
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        p.save()

        # not related to dry_run test, just for increasing coverage :)
        # create instance with single-entry history older than "minutes"
        # so it is skipped
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        h = p.history.first()
        h.history_date -= timedelta(days=31)
        h.save()

        self.assertEqual(Poll.history.all().count(), 3)
        out = StringIO()
        management.call_command(
            self.command_name,
            auto=True,
            days=20,
            dry=True,
            stdout=out,
            stderr=StringIO(),
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.Poll'>\n",
        )
        self.assertEqual(Poll.history.all().count(), 3)

    def test_auto_cleanup(self):
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.save()
        self.assertEqual(Poll.history.all().count(), 2)
        p.question = "Maybe this one won't...?"
        p.save()
        self.assertEqual(Poll.history.all().count(), 3)
        out = StringIO()
        h = p.history.first()
        h.history_date -= timedelta(days=40)
        h.save()
        management.call_command(
            self.command_name, auto=True, stdout=out, stderr=StringIO()
        )
        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.Poll'>\n",
        )
        self.assertEqual(Poll.history.all().count(), 2)

    def test_auto_cleanup_verbose(self):
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=datetime.now()
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.save()
        p.question = "Maybe this one won't...?"
        p.save()
        h = p.history.first()
        h.history_date -= timedelta(days=40)
        h.save()
        self.assertEqual(Poll.history.all().count(), 3)
        out = StringIO()
        management.call_command(
            self.command_name,
            "tests.poll",
            auto=True,
            verbosity=2,
            stdout=out,
            stderr=StringIO(),
        )

        self.assertEqual(
            out.getvalue(),
            "<class 'simple_history.tests.models.Poll'> has 1 old historical entries\n"
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.Poll'>\n",
        )
        self.assertEqual(Poll.history.all().count(), 2)

    def test_auto_cleanup_dated(self):
        the_time_is_now = datetime.now()
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=the_time_is_now
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.save()
        p.save()
        self.assertEqual(Poll.history.all().count(), 3)
        p.question = "Or this one...?"
        p.save()
        p.save()
        self.assertEqual(Poll.history.all().count(), 5)

        for h in Poll.history.all()[2:]:
            h.history_date -= timedelta(days=30)
            h.save()

        management.call_command(
            self.command_name,
            auto=True,
            days=20,
            stdout=StringIO(),
            stderr=StringIO(),
        )
        self.assertEqual(Poll.history.all().count(), 2)

    def test_auto_cleanup_dated_extra_one(self):
        the_time_is_now = datetime.now()
        p = Poll.objects.create(
            question="Will this be deleted?", pub_date=the_time_is_now
        )
        self.assertEqual(Poll.history.all().count(), 1)
        p.save()
        p.save()
        self.assertEqual(Poll.history.all().count(), 3)
        p.question = "Or this one...?"
        p.save()
        p.save()
        p.save()
        p.save()
        self.assertEqual(Poll.history.all().count(), 7)

        for h in Poll.history.all()[2:]:
            h.history_date -= timedelta(days=30)
            h.save()

        management.call_command(
            self.command_name,
            auto=True,
            days=20,
            stdout=StringIO(),
            stderr=StringIO(),
        )
        # We will remove the 3 ones that we are marking as old
        self.assertEqual(Poll.history.all().count(), 2)

    def test_auto_cleanup_custom_history_field(self):
        m = CustomManagerNameModel.objects.create(name="John")
        self.assertEqual(CustomManagerNameModel.log.all().count(), 1)
        m.save()
        self.assertEqual(CustomManagerNameModel.log.all().count(), 2)
        m.name = "Ivan"
        m.save()
        h = m.log.first()
        h.history_date -= timedelta(days=40)
        h.save()
        self.assertEqual(CustomManagerNameModel.log.all().count(), 3)
        out = StringIO()
        management.call_command(
            self.command_name, auto=True, stdout=out, stderr=StringIO()
        )

        self.assertEqual(
            out.getvalue(),
            "Removed 1 historical records for "
            "<class 'simple_history.tests.models.CustomManagerNameModel'>\n",
        )
        self.assertEqual(CustomManagerNameModel.log.all().count(), 2)


class TestCleanHistoryFields(TestCase):
    command_name = "clean_history_fields"

    REDACTED_EMAIL = "redacted@example.com"
    REDACTED_PHONE = "0000000000"

    def setUp(self):
        self.user = get_user_model().objects.create(username="operator")
        self.contact = Contact.objects.create(
            name="Alice",
            email="alice@example.com",
            phone="13800000000",
        )
        self.contact.email = "alice2@example.com"
        self.contact.phone = "13900000000"
        self.contact.save()
        self.other = Contact.objects.create(
            name="Bob",
            email="bob@example.com",
            phone="13700000000",
        )
        # Stamp audit columns so we can prove they survive the cleanup.
        Contact.history.all().update(
            history_user_id=self.user.pk, history_change_reason="original reason"
        )
        self.audit_snapshot = {
            h.history_id: (
                h.history_date,
                h.history_type,
                h.history_user_id,
                h.history_change_reason,
            )
            for h in Contact.history.all()
        }
        self.other_snapshot = list(
            Contact.history.filter(id=self.other.pk)
            .order_by("history_id")
            .values("name", "email", "phone")
        )

    def _call(self, *args, **kwargs):
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

    def _snapshot_other(self):
        return list(
            Contact.history.filter(id=self.other.pk)
            .order_by("history_id")
            .values("name", "email", "phone")
        )

    def _assert_audit_preserved(self):
        for history_id, audit in self.audit_snapshot.items():
            row = Contact.history.get(history_id=history_id)
            self.assertEqual(
                (
                    row.history_date,
                    row.history_type,
                    row.history_user_id,
                    row.history_change_reason,
                ),
                audit,
            )

    def test_dry_run_reports_counts_and_writes_nothing(self):
        before = list(
            Contact.history.filter(id=self.contact.pk)
            .order_by("history_id")
            .values("name", "email", "phone")
        )
        output, _ = self._call(
            "tests.contact",
            pks=str(self.contact.pk),
            fields=["email", "phone"],
            values=[
                f"email={self.REDACTED_EMAIL}",
                f"phone={self.REDACTED_PHONE}",
            ],
            dry_run=True,
        )
        self.assertIn("[dry-run]", output)
        self.assertIn("scanned=2", output)
        self.assertIn("to_change=2", output)
        # Nothing was modified in history.
        after = list(
            Contact.history.filter(id=self.contact.pk)
            .order_by("history_id")
            .values("name", "email", "phone")
        )
        self.assertEqual(before, after)
        # The live table is untouched.
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.email, "alice2@example.com")
        self.assertEqual(self.contact.phone, "13900000000")

    def test_apply_replaces_target_only_and_preserves_everything_else(self):
        output, _ = self._call(
            "tests.contact",
            pks=str(self.contact.pk),
            fields=["email", "phone"],
            values=[
                f"email={self.REDACTED_EMAIL}",
                f"phone={self.REDACTED_PHONE}",
            ],
        )
        self.assertIn("scanned=2", output)
        self.assertIn("changed=2", output)

        rows = list(
            Contact.history.filter(id=self.contact.pk).order_by("history_id")
        )
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row.email, self.REDACTED_EMAIL)
            self.assertEqual(row.phone, self.REDACTED_PHONE)
            # Unselected field kept verbatim.
            self.assertEqual(row.name, "Alice")

        # Other objects keep their history verbatim.
        self.assertEqual(self._snapshot_other(), self.other_snapshot)
        # Audit identity columns kept verbatim.
        self._assert_audit_preserved()
        # Live business table untouched.
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.email, "alice2@example.com")
        self.assertEqual(self.contact.phone, "13900000000")
        self.other.refresh_from_db()
        self.assertEqual(self.other.email, "bob@example.com")
        self.assertEqual(self.other.phone, "13700000000")

    def test_repeated_runs_are_idempotent(self):
        kwargs = dict(
            pks=str(self.contact.pk),
            fields=["email"],
            value=self.REDACTED_EMAIL,
            batchsize=1,
        )
        output, _ = self._call("tests.contact", **kwargs)
        self.assertIn("changed=2", output)
        output, _ = self._call("tests.contact", **kwargs)
        self.assertIn("scanned=2", output)
        self.assertIn("changed=0", output)
        for row in Contact.history.filter(id=self.contact.pk):
            self.assertEqual(row.email, self.REDACTED_EMAIL)

    def test_cutoff_bounds_which_rows_are_cleaned(self):
        history_ids = list(
            Contact.history.filter(id=self.contact.pk)
            .order_by("history_id")
            .values_list("history_id", flat=True)
        )
        old_id = history_ids[0]
        newer_id = history_ids[-1]
        HistoricalContact = Contact.history.model
        HistoricalContact.objects.filter(history_id=old_id).update(
            history_date=datetime(2000, 1, 1, 0, 0, 0)
        )
        # Anything dated after the fixed boundary (including rows written by a
        # concurrent process later) must be left alone.
        self._call(
            "tests.contact",
            pks=str(self.contact.pk),
            fields=["email"],
            value=self.REDACTED_EMAIL,
            cutoff="2001-01-01T00:00:00",
        )
        self.assertEqual(
            Contact.history.get(history_id=old_id).email, self.REDACTED_EMAIL
        )
        self.assertEqual(
            Contact.history.get(history_id=newer_id).email, "alice2@example.com"
        )

    def test_resume_after_history_id_skips_prefix(self):
        contact = Contact.objects.create(
            name="Carol", email="carol@example.com", phone="13600000000"
        )
        for index in range(4):
            contact.email = f"carol{index}@example.com"
            contact.save()
        ids = list(
            Contact.history.filter(id=contact.pk)
            .order_by("history_id")
            .values_list("history_id", flat=True)
        )
        self.assertEqual(len(ids), 5)
        cursor = ids[1]
        output, _ = self._call(
            "tests.contact",
            pks=str(contact.pk),
            fields=["email"],
            value=self.REDACTED_EMAIL,
            batchsize=2,
            resume_after_history_id=str(cursor),
        )
        # The resume cursor is reported back so runs can be continued.
        self.assertIn(f"resume_after_history_id={ids[-1]}", output)
        self.assertEqual(
            Contact.history.get(history_id=ids[0]).email, "carol@example.com"
        )
        self.assertEqual(
            Contact.history.get(history_id=ids[1]).email, "carol0@example.com"
        )
        for history_id in ids[2:]:
            self.assertEqual(
                Contact.history.get(history_id=history_id).email,
                self.REDACTED_EMAIL,
            )
        # A full re-run scrubs the skipped prefix as well; reruns converge.
        output, _ = self._call(
            "tests.contact",
            pks=str(contact.pk),
            fields=["email"],
            value=self.REDACTED_EMAIL,
        )
        for history_id in ids:
            self.assertEqual(
                Contact.history.get(history_id=history_id).email,
                self.REDACTED_EMAIL,
            )
        # Second full run has nothing left to do.
        output, _ = self._call(
            "tests.contact",
            pks=str(contact.pk),
            fields=["email"],
            value=self.REDACTED_EMAIL,
        )
        self.assertIn("changed=0", output)

    def test_null_replacement_rejected_for_not_null_field(self):
        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.contact",
                pks=str(self.contact.pk),
                fields=["name"],
                null=True,
            )
        self.assertIn("NOT NULL", str(ctx.exception))

    def test_null_replacement_accepted_for_nullable_field(self):
        self._call(
            "tests.contact",
            pks=str(self.contact.pk),
            fields=["phone"],
            null=True,
        )
        for row in Contact.history.filter(id=self.contact.pk):
            self.assertIsNone(row.phone)

    def test_audit_fields_are_protected(self):
        for field_name in ("history_id", "history_date", "history_user_id"):
            with self.subTest(field=field_name):
                with self.assertRaises(management.CommandError) as ctx:
                    self._call(
                        "tests.contact",
                        pks=str(self.contact.pk),
                        fields=[field_name],
                        value="x",
                    )
                self.assertIn("audit identity field", str(ctx.exception))

    def test_unknown_and_relation_fields_rejected(self):
        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.contact",
                pks=str(self.contact.pk),
                fields=["does_not_exist"],
                value="x",
            )
        self.assertIn("no concrete field named", str(ctx.exception))

        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.choice",
                pks="1",
                fields=["poll"],
                value="1",
            )
        self.assertIn("relation field", str(ctx.exception))

    def test_invalid_replacement_value_rejected(self):
        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.contact",
                pks=str(self.contact.pk),
                fields=["email"],
                value="not-an-email",
            )
        self.assertIn("Replacement value for 'email' is invalid", str(ctx.exception))

        poll = Poll.objects.create(
            question="q", pub_date=datetime(2020, 1, 1)
        )
        choice = Choice.objects.create(poll=poll, choice="c", votes=1)
        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.choice",
                pks=str(choice.pk),
                fields=["votes"],
                value="not-a-number",
            )
        self.assertIn("Replacement value for 'votes' is invalid", str(ctx.exception))

    def test_invalid_values_pairs_rejected(self):
        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.contact",
                pks=str(self.contact.pk),
                fields=["email", "phone"],
                values=["email=a@example.com"],
            )
        self.assertIn("exactly one value", str(ctx.exception))

        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.contact",
                pks=str(self.contact.pk),
                fields=["email"],
                values=["not-a-pair"],
            )
        self.assertIn("field=value", str(ctx.exception))

    def test_invalid_pks_rejected(self):
        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.contact",
                pks="not-a-number",
                fields=["email"],
                value=self.REDACTED_EMAIL,
            )
        self.assertIn("Invalid --pks", str(ctx.exception))

    def test_model_resolution_errors(self):
        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "invalid.model",
                pks="1",
                fields=["email"],
                value=self.REDACTED_EMAIL,
            )
        self.assertIn("Unable to find model", str(ctx.exception))

        with self.assertRaises(management.CommandError) as ctx:
            self._call(
                "tests.place",
                pks="1",
                fields=["name"],
                value="x",
            )
        self.assertIn("No history model found", str(ctx.exception))

    def test_required_arguments(self):
        # argparse errors surface as SystemExit on the CLI and CommandError when
        # invoked via call_command; both are acceptable.
        with self.assertRaises((SystemExit, management.CommandError)):
            self._call("tests.contact", fields=["email"], value="x")
        with self.assertRaises((SystemExit, management.CommandError)):
            self._call("tests.contact", pks="1")

    def test_output_never_contains_original_sensitive_values(self):
        output, error_output = self._call(
            "tests.contact",
            pks=str(self.contact.pk),
            fields=["email", "phone"],
            values=[
                f"email={self.REDACTED_EMAIL}",
                f"phone={self.REDACTED_PHONE}",
            ],
            verbosity=2,
        )
        for secret in (
            "alice@example.com",
            "alice2@example.com",
            "13800000000",
            "13900000000",
        ):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret, error_output)
