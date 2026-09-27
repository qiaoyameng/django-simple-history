import copy
from datetime import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from simple_history.exceptions import RestoreBlockedError, RestoreConflictError
from simple_history.restore import (
    ACTION_CREATE,
    ACTION_UPDATE,
    DUPLICATE_VERSION,
    FIELD_REMOVED,
    FK_TARGET_DELETED,
    HISTORY_NOT_FOUND,
    bulk_restore,
    plan_bulk_restore,
)
from simple_history.tests.models import (
    Choice,
    Place,
    Poll,
    PollPlace,
    PollWithCustomThrough,
    PollWithManyToMany,
    PollWithManyToManyCustomHistoryID,
    Voter,
)

User = get_user_model()


def make_poll(question="question", **kwargs):
    return Poll.objects.create(
        question=question, pub_date=datetime(2020, 1, 1), **kwargs
    )


class PlanBulkRestoreTestCase(TestCase):
    def test_plan_lists_updates(self):
        poll = make_poll(question="original")
        poll.question = "changed"
        poll.save()
        first_record = Poll.history.order_by("history_id").first()

        plan = plan_bulk_restore([first_record.history_id], Poll.history.model)

        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.actions), 1)
        action = plan.actions[0]
        self.assertEqual(action.action, ACTION_UPDATE)
        self.assertEqual(action.instance_pk, poll.pk)
        self.assertEqual(len(action.field_changes), 1)
        change = action.field_changes[0]
        self.assertEqual(change.field, "question")
        self.assertEqual(change.current, "changed")
        self.assertEqual(change.target, "original")
        self.assertEqual(plan.updates, [action])
        self.assertEqual(plan.creates, [])

    def test_plan_lists_creates_for_deleted_objects(self):
        poll = make_poll()
        poll_pk = poll.pk
        record = Poll.history.order_by("history_id").first()
        poll.delete()

        plan = plan_bulk_restore([record.history_id], Poll.history.model)

        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.creates), 1)
        self.assertEqual(plan.creates[0].action, ACTION_CREATE)
        self.assertEqual(plan.creates[0].instance_pk, poll_pk)

    def test_plan_lists_relation_replacements(self):
        place1 = Place.objects.create(name="place1")
        place2 = Place.objects.create(name="place2")
        poll = PollWithManyToMany.objects.create(
            question="q", pub_date=datetime(2020, 1, 1)
        )
        poll.places.add(place1)
        record = poll.history.order_by("-history_id").first()
        poll.places.set([place2])

        plan = plan_bulk_restore(
            [record.history_id], PollWithManyToMany.history.model
        )

        self.assertTrue(plan.ok)
        self.assertEqual(len(plan.relation_replacements), 1)
        m2m_change = plan.relation_replacements[0].m2m_changes[0]
        self.assertEqual(m2m_change.field, "places")
        self.assertEqual(m2m_change.current, [place2.pk])
        self.assertEqual(m2m_change.target, [place1.pk])

    def test_blocker_for_unknown_history_id(self):
        plan = plan_bulk_restore([12345], Poll.history.model)

        self.assertFalse(plan.ok)
        self.assertEqual(plan.blockers[0].code, HISTORY_NOT_FOUND)

    def test_blocker_for_duplicate_versions_of_same_object(self):
        poll = make_poll(question="one")
        poll.question = "two"
        poll.save()
        ids = list(
            Poll.history.order_by("history_id").values_list("history_id", flat=True)
        )
        self.assertEqual(len(ids), 2)

        plan = plan_bulk_restore(ids, Poll.history.model)

        self.assertFalse(plan.ok)
        self.assertEqual(len(plan.blockers), 2)
        self.assertTrue(
            all(b.code == DUPLICATE_VERSION for b in plan.blockers)
        )
        self.assertEqual(plan.actions, [])

    def test_blocker_for_deleted_fk_target(self):
        poll = make_poll()
        choice = Choice.objects.create(poll=poll, choice="yes", votes=0)
        user = User.objects.create(username="voter")
        voter = Voter.objects.create(user=user, choice=choice)
        record = Voter.history.model.objects.get(choice_id=choice.pk)
        choice.delete()  # cascades to the voter as well

        plan = plan_bulk_restore([record.history_id], Voter.history.model)

        self.assertFalse(plan.ok)
        codes = {b.code for b in plan.blockers}
        self.assertIn(FK_TARGET_DELETED, codes)

    def test_blocker_for_field_removed_from_model(self):
        poll = make_poll()
        record = Poll.history.first()
        history_model = Poll.history.model
        phantom = copy.copy(Poll._meta.get_field("question"))
        phantom.name = "obsolete_field"
        phantom.attname = "obsolete_field"
        tracked = list(history_model.tracked_fields) + [phantom]

        with patch.object(history_model, "tracked_fields", tracked):
            plan = plan_bulk_restore([record.history_id], history_model)

        self.assertFalse(plan.ok)
        self.assertEqual(plan.blockers[0].code, FIELD_REMOVED)
        self.assertIn("obsolete_field", plan.blockers[0].message)

    def test_blocked_plan_cannot_be_applied(self):
        plan = plan_bulk_restore([12345], Poll.history.model)
        with self.assertRaises(RestoreBlockedError):
            plan.apply()


class ApplyRestorePlanTestCase(TestCase):
    def test_apply_restores_scalars_and_writes_new_history(self):
        poll = make_poll(question="original")
        poll.question = "changed"
        poll.save()
        record = Poll.history.order_by("history_id").first()
        history_before = list(
            Poll.history.order_by("history_id").values(
                "history_id", "question", "history_change_reason"
            )
        )

        plan = plan_bulk_restore([record.history_id], Poll.history.model)
        plan.apply()

        poll.refresh_from_db()
        self.assertEqual(poll.question, "original")
        # Exactly one new history record, referencing the source history id
        self.assertEqual(Poll.history.count(), 3)
        new_record = Poll.history.order_by("-history_id").first()
        self.assertEqual(new_record.question, "original")
        self.assertIn(str(record.history_id), new_record.history_change_reason)
        # Original history is untouched
        history_after = list(
            Poll.history.order_by("history_id").values(
                "history_id", "question", "history_change_reason"
            )
        )
        self.assertEqual(history_after[: len(history_before)], history_before)

    def test_apply_recreates_deleted_object(self):
        poll = make_poll(question="back from the dead")
        poll_pk = poll.pk
        record = Poll.history.order_by("history_id").first()
        poll.delete()
        self.assertFalse(Poll.objects.filter(pk=poll_pk).exists())

        bulk_restore([record.history_id], Poll.history.model)

        restored = Poll.objects.get(pk=poll_pk)
        self.assertEqual(restored.question, "back from the dead")

    def test_apply_restores_m2m_relations(self):
        place1 = Place.objects.create(name="place1")
        place2 = Place.objects.create(name="place2")
        poll = PollWithManyToMany.objects.create(
            question="q", pub_date=datetime(2020, 1, 1)
        )
        poll.places.add(place1, place2)
        record = poll.history.order_by("-history_id").first()
        poll.places.clear()

        bulk_restore([record.history_id], PollWithManyToMany.history.model)

        self.assertEqual(
            set(poll.places.values_list("pk", flat=True)), {place1.pk, place2.pk}
        )
        # The new history record carries the restored M2M snapshot
        m2m_history_model = getattr(PollWithManyToMany.history.model, "places").model
        new_record = poll.history.order_by("-history_id").first()
        snapshot = m2m_history_model.objects.filter(history=new_record)
        self.assertEqual(
            {row.place_id for row in snapshot}, {place1.pk, place2.pk}
        )
        self.assertIn(str(record.history_id), new_record.history_change_reason)

    def test_apply_restores_custom_through_fields(self):
        place1 = Place.objects.create(name="place1")
        place2 = Place.objects.create(name="place2")
        poll = PollWithCustomThrough.objects.create(question="q")
        poll.places.add(place1, through_defaults={"role": "review"})
        record = poll.history.order_by("-history_id").first()
        poll.places.clear()
        poll.places.add(place2, through_defaults={"role": "final"})

        bulk_restore([record.history_id], PollWithCustomThrough.history.model)

        rows = list(PollPlace.objects.filter(poll=poll))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].place_id, place1.pk)
        self.assertEqual(rows[0].role, "review")

    def test_apply_records_user_and_custom_change_reason(self):
        poll = make_poll(question="original")
        poll.question = "changed"
        poll.save()
        record = Poll.history.order_by("history_id").first()
        user = User.objects.create(username="operator")

        bulk_restore(
            [record.history_id],
            Poll.history.model,
            user=user,
            change_reason="ops restore",
        )

        new_record = Poll.history.order_by("-history_id").first()
        self.assertEqual(new_record.history_user_id, user.pk)
        self.assertEqual(new_record.history_change_reason, "ops restore")

    def test_apply_detects_drift_and_rolls_back_everything(self):
        poll1 = make_poll(question="one-original")
        poll2 = make_poll(question="two-original")
        poll1.question = "one-changed"
        poll1.save()
        poll2.question = "two-changed"
        poll2.save()
        record1 = Poll.history.filter(question="one-original").first()
        record2 = Poll.history.filter(question="two-original").first()

        plan = plan_bulk_restore(
            [record1.history_id, record2.history_id], Poll.history.model
        )
        self.assertTrue(plan.ok)

        # The object drifts after the plan was generated
        poll2.question = "two-drifted"
        poll2.save()

        with self.assertRaises(RestoreConflictError):
            plan.apply()

        poll1.refresh_from_db()
        poll2.refresh_from_db()
        # Nothing was applied: poll1 was not restored, poll2 kept its new value
        self.assertEqual(poll1.question, "one-changed")
        self.assertEqual(poll2.question, "two-drifted")
        # No restore history records were written
        self.assertFalse(
            Poll.history.filter(
                history_change_reason__contains="Restored from history_id="
            ).exists()
        )

    def test_apply_detects_deleted_object_drift(self):
        poll = make_poll(question="original")
        poll.question = "changed"
        poll.save()
        poll_pk = poll.pk
        record = Poll.history.order_by("history_id").first()
        plan = plan_bulk_restore([record.history_id], Poll.history.model)
        poll.delete()

        with self.assertRaises(RestoreConflictError):
            plan.apply()
        self.assertFalse(Poll.objects.filter(pk=poll_pk).exists())

    def test_apply_detects_new_object_drift(self):
        poll = make_poll()
        poll_pk = poll.pk
        record = Poll.history.order_by("history_id").first()
        poll.delete()
        plan = plan_bulk_restore([record.history_id], Poll.history.model)
        # The same pk reappears after the plan was generated
        Poll.objects.create(
            pk=poll_pk,
            question="impostor",
            pub_date=datetime(2020, 1, 1),
        )

        with self.assertRaises(RestoreConflictError):
            plan.apply()
        self.assertEqual(Poll.objects.get(pk=poll_pk).question, "impostor")

    def test_bulk_restore_raises_for_blocked_plan(self):
        with self.assertRaises(RestoreBlockedError):
            bulk_restore([12345], Poll.history.model)


class CustomHistoryIdRestoreTestCase(TestCase):
    def test_restore_with_uuid_history_ids(self):
        place = Place.objects.create(name="place")
        poll = PollWithManyToManyCustomHistoryID.objects.create(
            question="original", pub_date=datetime(2020, 1, 1)
        )
        poll.places.add(place)
        record = poll.history.order_by("-history_date", "-history_id").first()
        poll.question = "changed"
        poll.save()
        poll.places.clear()

        plan = plan_bulk_restore(
            [record.history_id], PollWithManyToManyCustomHistoryID.history.model
        )
        self.assertTrue(plan.ok)
        plan.apply()

        poll.refresh_from_db()
        self.assertEqual(poll.question, "original")
        self.assertEqual(list(poll.places.values_list("pk", flat=True)), [place.pk])
