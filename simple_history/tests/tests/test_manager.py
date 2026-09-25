from datetime import datetime, timedelta
from operator import attrgetter

from django.contrib.auth import get_user_model
from django.core.exceptions import FieldError
from django.db import IntegrityError
from django.test import TestCase, override_settings, skipUnlessDBFeature

from simple_history.manager import SIMPLE_HISTORY_REVERSE_ATTR_NAME

from ..models import Choice, Document, Poll, RankedDocument
from .utils import HistoricalTestCase

User = get_user_model()


class LatestOfEachTestCase(HistoricalTestCase):
    def test_filtered_instances_are_as_expected(self):
        document1 = RankedDocument.objects.create(rank=10)
        document2 = RankedDocument.objects.create(rank=20)
        document2.rank = 21
        document2.save()
        document3 = RankedDocument.objects.create(rank=30)
        document3.rank = 31
        document3.save()
        document3.delete()
        document4 = RankedDocument.objects.create(rank=40)
        document4_pk = document4.pk
        document4.delete()
        reincarnated_document4 = RankedDocument.objects.create(pk=document4_pk, rank=42)

        record4, record3, record2, record1 = RankedDocument.history.latest_of_each()
        self.assertRecordValues(
            record1,
            RankedDocument,
            {
                "rank": 10,
                "id": document1.id,
                "history_type": "+",
            },
        )
        self.assertRecordValues(
            record2,
            RankedDocument,
            {
                "rank": 21,
                "id": document2.id,
                "history_type": "~",
            },
        )
        self.assertRecordValues(
            record3,
            RankedDocument,
            {
                "rank": 31,
                "id": document3.id,
                "history_type": "-",
            },
        )
        self.assertRecordValues(
            record4,
            RankedDocument,
            {
                "rank": 42,
                "id": reincarnated_document4.id,
                "history_type": "+",
            },
        )


class AsOfTestCase(TestCase):
    model = Document

    def setUp(self):
        user = User.objects.create_user("tester", "tester@example.com")
        self.now = datetime.now()
        self.yesterday = self.now - timedelta(days=1)
        self.obj = self.model.objects.create()
        self.obj.changed_by = user
        self.obj.save()
        self.model.objects.all().delete()  # allows us to leave PK on instance
        (
            self.delete_history,
            self.change_history,
            self.create_history,
        ) = self.model.history.all()
        self.create_history.history_date = self.now - timedelta(days=2)
        self.create_history.save()
        self.change_history.history_date = self.now - timedelta(days=1)
        self.change_history.save()
        self.delete_history.history_date = self.now
        self.delete_history.save()

    def test_created_after(self):
        """An object created after the 'as of' date should not be
        included.

        """
        as_of_list = list(self.model.history.as_of(self.now - timedelta(days=5)))
        self.assertFalse(as_of_list)

    def test_deleted_before(self):
        """An object deleted before the 'as of' date should not be
        included.

        """
        as_of_list = list(self.model.history.as_of(self.now + timedelta(days=1)))
        self.assertFalse(as_of_list)

    def test_deleted_after(self):
        """An object created before, but deleted after the 'as of'
        date should be included.

        """
        as_of_list = list(self.model.history.as_of(self.now - timedelta(days=1)))
        self.assertEqual(len(as_of_list), 1)
        self.assertEqual(as_of_list[0].pk, self.obj.pk)

    def test_modified(self):
        """An object modified before the 'as of' date should reflect
        the last version.

        """
        as_of_list = list(self.model.history.as_of(self.now - timedelta(days=1)))
        self.assertEqual(as_of_list[0].changed_by, self.obj.changed_by)


class AsOfTestCaseWithoutSetUp(TestCase):
    def test_create_and_delete(self):
        document = Document.objects.create()
        now = datetime.now()
        document.delete()

        docs_as_of_now = Document.history.as_of(now)
        doc = docs_as_of_now[0]
        # as_of queries inject a property allowing callers
        # to go from instance to historical instance
        historic = getattr(doc, SIMPLE_HISTORY_REVERSE_ATTR_NAME)
        self.assertIsNotNone(historic)
        # as_of queries inject the time point of the original
        # query into the historic record so callers can do magical
        # things like chase historic foreign key relationships
        # by patching forward and reverse one-to-one relationship
        # processing (see issue 880)
        self.assertEqual(historic._as_of, now)

        docs_as_of_tmw = Document.history.as_of(now + timedelta(days=1))
        with self.assertNumQueries(1):
            self.assertFalse(list(docs_as_of_tmw))

    def test_multiple(self):
        document1 = Document.objects.create()
        document2 = Document.objects.create()
        historical = Document.history.as_of(datetime.now() + timedelta(days=1))
        # history, even converted to objects, is kept in reverse chronological order
        # because sorting it based on the original table's meta ordering is not possible
        # when the ordering leverages foreign key relationships
        with self.assertNumQueries(1):
            self.assertEqual(list(historical), [document2, document1])

    def test_filter_pk_as_instance(self):
        # when a queryset is returning historical documents, `pk` queries
        # reference the history_id; however when a queryset is returning
        # instances, `pk' queries reference the original table's primary key
        document1 = RankedDocument.objects.create(id=101, rank=42)
        RankedDocument.objects.create(id=102, rank=84)
        self.assertFalse(RankedDocument.history.filter(pk=document1.id))
        self.assertTrue(
            RankedDocument.history.all().as_instances().filter(pk=document1.id)
        )

    def test_as_of(self):
        """Demonstrates how as_of works now that it returns a QuerySet."""
        t0 = datetime.now()
        document1 = RankedDocument.objects.create(rank=42)
        document2 = RankedDocument.objects.create(rank=84)
        t1 = datetime.now()
        document2.rank = 51
        document2.save()
        document1.delete()
        t2 = datetime.now()

        # nothing exists at t0
        queryset = RankedDocument.history.as_of(t0)
        self.assertEqual(queryset.count(), 0)

        # at t1, two records exist
        queryset = RankedDocument.history.as_of(t1)
        self.assertEqual(queryset.count(), 2)
        self.assertEqual(queryset.filter(rank__gte=75).count(), 1)
        ids = {item["id"] for item in queryset.values("id")}
        self.assertEqual(ids, {document1.id, document2.id})

        # these records are historic
        record = queryset[0]
        historic = getattr(record, SIMPLE_HISTORY_REVERSE_ATTR_NAME)
        self.assertIsInstance(historic, RankedDocument.history.model)
        self.assertEqual(historic._as_of, t1)

        # at t2 we have one record left
        queryset = RankedDocument.history.as_of(t2)
        self.assertEqual(queryset.count(), 1)
        self.assertEqual(queryset.filter(rank__gte=75).count(), 0)

    def test_historical_query_set(self):
        """
        Demonstrates how the HistoricalQuerySet works to provide as_of functionality.
        """
        document1 = RankedDocument.objects.create(rank=10)
        document2 = RankedDocument.objects.create(rank=20)
        document2.rank = 21
        document2.save()
        document1.delete()
        t1 = datetime.now()
        document3 = RankedDocument.objects.create(rank=30)  # noqa: F841
        document2.rank = 22
        document2.save()
        t2 = datetime.now()

        # 4 records before `t1` (for document 1 and 2), 2 after (for document 2 and 3)
        queryset = RankedDocument.history.filter(history_date__lte=t1)
        self.assertEqual(queryset.count(), 4)
        self.assertEqual(RankedDocument.history.filter(history_date__gt=t1).count(), 2)

        # `latest_of_each()` returns the most recent record of each document
        with self.assertNumQueries(1):
            self.assertEqual(queryset.latest_of_each().count(), 2)

        # `as_instances()` returns the historical instances as of each record's time,
        # but excludes deletion records (i.e. document 1's most recent record)
        with self.assertNumQueries(1):
            self.assertEqual(queryset.latest_of_each().as_instances().count(), 1)

        # (Duplicate calls to these methods should not change the number of queries,
        # since they're idempotent)
        with self.assertNumQueries(1):
            self.assertEqual(
                queryset.latest_of_each()
                .latest_of_each()
                .as_instances()
                .as_instances()
                .count(),
                1,
            )

        self.assertSetEqual(
            # In conclusion, all of these methods combined...
            set(
                RankedDocument.history.filter(history_date__lte=t1)
                .latest_of_each()
                .as_instances()
            ),
            # ...are equivalent to calling `as_of()`!
            set(RankedDocument.history.as_of(t1)),
        )

        self.assertEqual(RankedDocument.history.as_of(t1).get().rank, 21)
        self.assertListEqual(
            [d.rank for d in RankedDocument.history.as_of(t2)], [22, 30]
        )


class BulkHistoryCreateTestCase(TestCase):
    def setUp(self):
        self.data = [
            Poll(id=1, question="Question 1", pub_date=datetime.now()),
            Poll(id=2, question="Question 2", pub_date=datetime.now()),
            Poll(id=3, question="Question 3", pub_date=datetime.now()),
            Poll(id=4, question="Question 4", pub_date=datetime.now()),
        ]

    def test_simple_bulk_history_create(self):
        created = Poll.history.bulk_history_create(self.data)
        self.assertEqual(len(created), 4)
        self.assertQuerySetEqual(
            Poll.history.order_by("question"),
            ["Question 1", "Question 2", "Question 3", "Question 4"],
            attrgetter("question"),
        )
        self.assertTrue(
            all([history.history_type == "+" for history in Poll.history.all()])
        )

        created = Poll.history.bulk_create([])
        self.assertEqual(created, [])
        self.assertEqual(Poll.history.count(), 4)

    @override_settings(SIMPLE_HISTORY_ENABLED=False)
    def test_simple_bulk_history_create_without_history_enabled(self):
        Poll.history.bulk_history_create(self.data)
        self.assertEqual(Poll.history.count(), 0)

    def test_bulk_history_create_with_change_reason(self):
        for poll in self.data:
            poll._change_reason = "reason"

        Poll.history.bulk_history_create(self.data)

        self.assertTrue(
            all(
                [
                    history.history_change_reason == "reason"
                    for history in Poll.history.all()
                ]
            )
        )

    def test_bulk_history_create_with_default_user(self):
        user = User.objects.create_user("tester", "tester@example.com")

        Poll.history.bulk_history_create(self.data, default_user=user)

        self.assertTrue(
            all([history.history_user == user for history in Poll.history.all()])
        )

    def test_bulk_history_create_with_default_change_reason(self):
        Poll.history.bulk_history_create(self.data, default_change_reason="test")

        self.assertTrue(
            all(
                [
                    history.history_change_reason == "test"
                    for history in Poll.history.all()
                ]
            )
        )

    def test_bulk_history_create_history_user_overrides_default(self):
        user1 = User.objects.create_user("tester1", "tester1@example.com")
        user2 = User.objects.create_user("tester2", "tester2@example.com")

        for data in self.data:
            data._history_user = user1

        Poll.history.bulk_history_create(self.data, default_user=user2)

        self.assertTrue(
            all([history.history_user == user1 for history in Poll.history.all()])
        )

    def test_bulk_history_create_change_reason_overrides_default(self):
        for data in self.data:
            data._change_reason = "my_reason"

        Poll.history.bulk_history_create(self.data, default_change_reason="test")

        self.assertTrue(
            all(
                [
                    history.history_change_reason == "my_reason"
                    for history in Poll.history.all()
                ]
            )
        )

    def test_bulk_history_create_on_objs_without_ids(self):
        self.data = [
            Poll(question="Question 1", pub_date=datetime.now()),
            Poll(question="Question 2", pub_date=datetime.now()),
            Poll(question="Question 3", pub_date=datetime.now()),
            Poll(question="Question 4", pub_date=datetime.now()),
        ]

        with self.assertRaises(IntegrityError):
            Poll.history.bulk_history_create(self.data)

    def test_set_custom_history_date_on_first_obj(self):
        self.data[0]._history_date = datetime(2000, 1, 1)

        Poll.history.bulk_history_create(self.data)

        self.assertEqual(
            Poll.history.order_by("question")[0].history_date, datetime(2000, 1, 1)
        )

    def test_set_custom_history_user_on_first_obj(self):
        user = User.objects.create_user("tester", "tester@example.com")
        self.data[0]._history_user = user

        Poll.history.bulk_history_create(self.data)

        self.assertEqual(Poll.history.order_by("question")[0].history_user, user)

    @skipUnlessDBFeature("has_bulk_insert")
    def test_efficiency(self):
        with self.assertNumQueries(1):
            Poll.history.bulk_history_create(self.data)


class BulkHistoryUpdateTestCase(TestCase):
    def setUp(self):
        self.data = [
            Poll(id=1, question="Question 1", pub_date=datetime.now()),
            Poll(id=2, question="Question 2", pub_date=datetime.now()),
            Poll(id=3, question="Question 3", pub_date=datetime.now()),
            Poll(id=4, question="Question 4", pub_date=datetime.now()),
        ]

    def test_simple_bulk_history_create(self):
        created = Poll.history.bulk_history_create(self.data, update=True)
        self.assertEqual(len(created), 4)
        self.assertQuerySetEqual(
            Poll.history.order_by("question"),
            ["Question 1", "Question 2", "Question 3", "Question 4"],
            attrgetter("question"),
        )
        self.assertTrue(
            all([history.history_type == "~" for history in Poll.history.all()])
        )

        created = Poll.history.bulk_create([])
        self.assertEqual(created, [])
        self.assertEqual(Poll.history.count(), 4)

    def test_bulk_history_create_with_change_reason(self):
        for poll in self.data:
            poll._change_reason = "reason"

        Poll.history.bulk_history_create(self.data)

        self.assertTrue(
            all(
                [
                    history.history_change_reason == "reason"
                    for history in Poll.history.all()
                ]
            )
        )


class PrefetchingMethodsTestCase(TestCase):
    def setUp(self):
        d = datetime(3021, 1, 1, 10, 0)
        self.poll1 = Poll.objects.create(question="why?", pub_date=d)
        self.poll2 = Poll.objects.create(question="how?", pub_date=d)
        self.choice1 = Choice.objects.create(poll=self.poll1, votes=1)
        self.choice2 = Choice.objects.create(poll=self.poll1, votes=2)
        self.choice3 = Choice.objects.create(poll=self.poll2, votes=3)

    def test__select_related_history_tracked_objs__prefetches_expected_objects(self):
        num_choices = Choice.objects.count()
        self.assertEqual(num_choices, 3)

        def access_related_objs(records):
            for record in records:
                self.assertIsInstance(record.poll, Poll)

        # Without prefetching:
        with self.assertNumQueries(1):
            historical_records = Choice.history.all()
            self.assertEqual(len(historical_records), num_choices)
        with self.assertNumQueries(num_choices):
            access_related_objs(historical_records)

        # With prefetching:
        with self.assertNumQueries(1):
            historical_records = (
                Choice.history.all()._select_related_history_tracked_objs()
            )
            self.assertEqual(len(historical_records), num_choices)
        with self.assertNumQueries(0):
            access_related_objs(historical_records)


class DiffAsOfTestCase(TestCase):
    def setUp(self):
        self.pub_date = datetime(2020, 1, 1, 12, 0)
        self.new_pub_date = datetime(2021, 6, 1, 12, 0)

        # Objects existing before the diff window starts.
        self.unchanged = Poll.objects.create(
            question="keep", pub_date=self.pub_date
        )
        self.changed = Poll.objects.create(question="old", pub_date=self.pub_date)
        self.removed = Poll.objects.create(question="gone", pub_date=self.pub_date)
        self.recreated = Poll.objects.create(
            question="orig", pub_date=self.pub_date
        )
        self.recreated_same = Poll.objects.create(
            question="same", pub_date=self.pub_date
        )
        self.reverted = Poll.objects.create(question="a", pub_date=self.pub_date)
        self.same_ts = Poll.objects.create(question="v1", pub_date=self.pub_date)
        # Deleted before the window: should never show up in the diff.
        Poll.objects.create(question="early", pub_date=self.pub_date).delete()

        self.start = datetime.now()

        # Activity inside the diff window.
        self.changed.question = "new"
        self.changed.pub_date = self.new_pub_date
        self.changed.save()

        self.removed_pk = self.removed.pk
        self.removed.delete()

        self.added = Poll.objects.create(question="fresh", pub_date=self.pub_date)

        # Deleted and recreated under the same pk with different values.
        recreated_pk = self.recreated.pk
        self.recreated.delete()
        self.recreated = Poll.objects.create(
            pk=recreated_pk, question="rebuilt", pub_date=self.pub_date
        )

        # Deleted and recreated under the same pk with identical values.
        recreated_same_pk = self.recreated_same.pk
        self.recreated_same.delete()
        self.recreated_same = Poll.objects.create(
            pk=recreated_same_pk, question="same", pub_date=self.pub_date
        )

        # Changed and then changed back: endpoints are equal.
        self.reverted.question = "b"
        self.reverted.save()
        self.reverted.question = "a"
        self.reverted.save()

        # Two versions stamped at the exact same time; the record created
        # last (highest history id) must win.
        self.same_ts.question = "v2"
        self.same_ts.save()
        self.same_ts.question = "v3"
        self.same_ts.save()
        v2, v3 = Poll.history.filter(id=self.same_ts.pk, history_type="~").order_by(
            "history_id"
        )
        shared_ts = datetime.now()
        v2.history_date = shared_ts
        v2.save()
        v3.history_date = shared_ts
        v3.save()

        self.end = datetime.now()

    def history_id(self, obj, history_type):
        return Poll.history.get(id=obj.pk, history_type=history_type).history_id

    def test_diff_as_of_full_result(self):
        diff = Poll.history.diff_as_of(self.start, self.end)

        self.assertEqual([entry.pk for entry in diff.added], [self.added.pk])
        self.assertEqual(diff.added[0].start_history_id, None)
        self.assertEqual(
            diff.added[0].end_history_id, self.history_id(self.added, "+")
        )

        self.assertEqual([entry.pk for entry in diff.removed], [self.removed_pk])
        self.assertEqual(
            diff.removed[0].start_history_id,
            Poll.history.get(id=self.removed_pk, history_type="+").history_id,
        )
        self.assertEqual(diff.removed[0].end_history_id, None)

        self.assertEqual(
            [entry.pk for entry in diff.changed],
            [self.changed.pk, self.recreated.pk, self.same_ts.pk],
        )
        changed_entry, recreated_entry, same_ts_entry = diff.changed

        self.assertEqual(
            changed_entry.start_history_id, self.history_id(self.changed, "+")
        )
        self.assertEqual(
            changed_entry.end_history_id, self.history_id(self.changed, "~")
        )
        self.assertEqual(
            set(changed_entry.changes), {"question", "pub_date"}
        )
        self.assertEqual(changed_entry.changes["question"].before, "old")
        self.assertEqual(changed_entry.changes["question"].after, "new")
        self.assertEqual(changed_entry.changes["pub_date"].before, self.pub_date)
        self.assertEqual(
            changed_entry.changes["pub_date"].after, self.new_pub_date
        )

        # Deleted-then-recreated objects are compared by endpoint states.
        first_create = (
            Poll.history.filter(id=self.recreated.pk, history_type="+")
            .order_by("history_id")
            .first()
        )
        self.assertEqual(
            recreated_entry.start_history_id, first_create.history_id
        )
        self.assertEqual(recreated_entry.changes["question"].before, "orig")
        self.assertEqual(recreated_entry.changes["question"].after, "rebuilt")

        # The later of the two same-timestamp versions wins.
        self.assertEqual(same_ts_entry.changes["question"].before, "v1")
        self.assertEqual(same_ts_entry.changes["question"].after, "v3")
        self.assertEqual(
            same_ts_entry.end_history_id,
            Poll.history.filter(id=self.same_ts.pk)
            .order_by("history_id")
            .last()
            .history_id,
        )

        self.assertEqual(diff.added_total, 1)
        self.assertEqual(diff.removed_total, 1)
        self.assertEqual(diff.changed_total, 3)

    def test_diff_as_of_uses_constant_number_of_queries(self):
        with self.assertNumQueries(2):
            Poll.history.diff_as_of(self.start, self.end)

    def test_diff_as_of_field_selection(self):
        diff = Poll.history.diff_as_of(self.start, self.end, fields=["question"])
        changed_entry = diff.changed[0]
        self.assertEqual(changed_entry.pk, self.changed.pk)
        self.assertEqual(set(changed_entry.changes), {"question"})

        diff = Poll.history.diff_as_of(self.start, self.end, fields=["pub_date"])
        by_pk = {entry.pk: entry for entry in diff.changed}
        # Only `changed` differs on pub_date alone.
        self.assertEqual(set(by_pk), {self.changed.pk})
        self.assertEqual(set(by_pk[self.changed.pk].changes), {"pub_date"})

    def test_diff_as_of_unknown_field_raises(self):
        with self.assertRaises(FieldError):
            Poll.history.diff_as_of(self.start, self.end, fields=["nope"])

    def test_diff_as_of_filters(self):
        diff = Poll.history.diff_as_of(
            self.start, self.end, filters={"question__in": ["keep", "old", "new"]}
        )
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual([entry.pk for entry in diff.changed], [self.changed.pk])

    def test_diff_as_of_pagination(self):
        page1 = Poll.history.diff_as_of(self.start, self.end, page=1, page_size=2)
        self.assertEqual(
            [entry.pk for entry in page1.changed],
            [self.changed.pk, self.recreated.pk],
        )
        self.assertEqual(page1.changed_total, 3)

        page2 = Poll.history.diff_as_of(self.start, self.end, page=2, page_size=2)
        self.assertEqual(
            [entry.pk for entry in page2.changed], [self.same_ts.pk]
        )
        self.assertEqual(page2.changed_total, 3)

        unpaginated = Poll.history.diff_as_of(self.start, self.end)
        self.assertEqual(len(unpaginated.changed), 3)

    def test_diff_as_of_page_requires_page_size(self):
        with self.assertRaises(ValueError):
            Poll.history.diff_as_of(self.start, self.end, page=1)

    def test_diff_as_of_empty_window(self):
        diff = Poll.history.diff_as_of(self.end, datetime.now())
        self.assertEqual(diff.added, [])
        self.assertEqual(diff.removed, [])
        self.assertEqual(diff.changed, [])
