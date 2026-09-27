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

Sometimes historical records contain values that must disappear (for example a
user's email address or phone number left in older versions), while the audit
information - *when* the record changed and *who* made the change - must be
preserved. The ``clean_history_fields`` command rewrites selected fields of
selected objects' historical rows to a fixed replacement value. It never
touches the live business table and refuses to modify the audit identity
fields ``history_id``, ``history_date``, ``history_type``, ``history_user``
and ``history_change_reason`` (nor the tracked primary key or relation
fields).

The target model, business object ids, history fields and replacement value
must always be given explicitly:

.. code-block:: bash

    $ python manage.py clean_history_fields customers.customer \
        --fields email phone \
        --pks 42 43 \
        --replace "redacted@example.invalid"

By default the command runs as a **dry run** and only reports how many rows
would change, without writing anything. Add ``--apply`` to perform the
rewrite (``-d``/``--dry-run`` forces the preview). Nullable fields can be
cleared with ``--set-null`` instead of ``--replace``:

.. code-block:: bash

    $ python manage.py clean_history_fields customers.customer \
        --fields phone --pks 42 --set-null --apply

The run works against a **fixed upper boundary** on ``history_id``: either the
highest in-scope history id captured when the run starts, or an explicit
``--cutoff-history-id``. History rows inserted concurrently after that
boundary are never followed. Large tables are processed in batches
(``--batch-size``, default 500); the command prints a resume token after each
run. If a run is interrupted, re-run it with the same
``--cutoff-history-id`` plus ``--resume-after-history-id`` set to the last
reported id. The operation is idempotent: re-running it changes no rows that
were already rewritten, so partial runs can simply be continued or repeated.

The same functionality is available programmatically through
:func:`simple_history.cleanup.build_cleanup_spec` and
:func:`simple_history.cleanup.clean_history_fields`.
