Utils
=====


clean_duplicate_history
-----------------------

For performance reasons, ``django-simple-history`` always creates an ``HistoricalRecord``
when ``Model.save()`` is called regardless of data having actually changed.
If you find yourself with a lot of history duplicates you can schedule the
``clean_duplicate_history`` command

.. code-block:: bash

    $ python manage.py clean_duplicate_history --auto

You can use ``--auto`` to clean up duplicates for every model
with ``HistoricalRecords`` or enumerate specific models as args.
There is also ``-m/--minutes`` to specify how many minutes to go
back in history while searching (default checks whole history),
so you can schedule, for instance, an hourly cronjob such as

.. code-block:: bash

    $ python manage.py clean_duplicate_history -m 60 --auto

You can also use ``--excluded_fields`` to provide a list of fields to be excluded
from the duplicate check

.. code-block:: bash

    $ python manage.py clean_duplicate_history --auto --excluded_fields field1 field2

You can use Django's base manager to perform the cleanup over all records,
including those that would otherwise be filtered or modified by a
custom manager, by using the ``--base-manager`` flag.

.. code-block:: bash

    $ python manage.py clean_duplicate_history --auto --base-manager

clean_old_history
-----------------------

You may want to remove historical records that have existed for a certain amount of time.

If you find yourself with a lot of old history you can schedule the
``clean_old_history`` command

.. code-block:: bash

    $ python manage.py clean_old_history --auto

You can use ``--auto`` to remove old historical entries
with ``HistoricalRecords`` or enumerate specific models as args.
You may also specify a  ``--days`` parameter, which indicates how many
days of records you want to keep. The default it 30 days, meaning that
all records older than 30 days would be removed.

.. code-block:: bash

    $ python manage.py clean_old_history --days 60 --auto

bulk_restore
------------

``simple_history.restore`` provides a two-step bulk restore interface for
operations teams that need to roll a set of objects back to selected
historical versions. ``plan_bulk_restore()`` takes a list of history ids of
one history model and builds a checkable plan without writing anything:

.. code-block:: python

    from simple_history.restore import plan_bulk_restore

    plan = plan_bulk_restore(history_ids, Poll.history.model)
    if plan.ok:
        plan.apply()

The plan lists what will happen: ``plan.creates`` (objects that will be
re-created because they no longer exist), ``plan.updates`` (scalar field
changes), ``plan.relation_replacements`` (tracked many-to-many relations -
including custom ``through`` models and their extra fields - that will be
replaced with the historical rows) and ``plan.blockers``.

A plan is blocked (and cannot be applied) when a history id does not exist,
when two versions of the same object were selected, when a field stored in
the history no longer exists on the current model, or when a foreign key
target referenced by a selected version has been deleted. All of these are
detected while planning, never halfway through applying.

``plan.apply()`` re-checks that none of the objects have drifted from the
state the plan was generated from, then applies the whole plan in a single
transaction - it either succeeds completely or is rolled back. The original
history is never modified; every restored object gets one new history record
whose change reason references the source history id. ``bulk_restore()``
combines both steps and raises ``RestoreBlockedError`` for blocked plans.
