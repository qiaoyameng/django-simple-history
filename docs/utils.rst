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

clean_history_fields
--------------------

Sometimes historical rows retain data that must later be removed - for
example a user's email address or phone number left behind in old
``HistoricalRecord`` versions. The ``clean_history_fields`` management command
replaces selected field values in the history of explicitly selected objects
while preserving the audit identity of every row
(``history_id``, ``history_date``, ``history_user`` and
``history_change_reason`` are never modified).

The model, the set of business objects, the fields and the replacement values
must all be given explicitly:

.. code-block:: bash

    $ python manage.py clean_history_fields contacts.contact \
        --pks 42 \
        --fields email phone \
        --values email=redacted@example.com phone=0000000000 \
        --dry-run

Use ``--value`` to apply one replacement to every selected field, or
``--null`` to scrub nullable fields with ``NULL``. Always start with
``-d/--dry-run``: it performs every check and reports how many rows would
change without writing anything.

Before touching any row the command verifies that:

- the model is historical and the fields are plain scalar model columns
  (relations, the primary key and audit fields are rejected),
- the replacement passes field validation (type, max length, format),
- a ``NULL`` replacement is only used on nullable columns.

Only historical rows are ever updated; the live business table is left
untouched. Original sensitive values are never printed, even on verbose
output.

Large history sets are processed in fixed-size batches
(``--batchsize``) inside individual transactions. Each batch reports a
``resume-after-history-id`` cursor which can be passed to a later invocation
with ``--resume-after-history-id`` to continue an interrupted run. Re-running
the command is idempotent: rows that already hold the replacement value are
skipped.

A fixed boundary is captured when the run starts (override it with
``--cutoff`` as an ISO-8601 datetime). Only rows with
``history_date <= cutoff`` are considered, so history written concurrently
while the command runs is never chased indefinitely. Reuse the same
``--cutoff`` when resuming.

.. code-block:: bash

    $ python manage.py clean_history_fields contacts.contact \
        --pks 42,43 \
        --fields email \
        --value redacted@example.com \
        --batchsize 500 \
        --cutoff 2026-01-01T00:00:00+00:00
