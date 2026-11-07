# Task priorities

Tasks get a priority so the urgent ones can be found quickly.

- A task has a priority from 0 to 3. 0 is the most urgent, 3 the least, and the default is 2.
  `taskboard add TITLE --priority N` sets it; other values are rejected.
- `taskboard list` shows the priority as `P<n>` before the title, and `taskboard list --sort
  priority` shows the most urgent tasks first (ties by id).
- The priority is saved in the JSON file. Files written by older versions have no priority and
  load with the default.
- The CSV export gets a `priority` column right after `title`.
