# Daily digest and local times for reminders

- `Agenda.digest(days, now=None)` returns the reminders due in the window `[now, now + days)`
  grouped by UTC calendar day (`"YYYY-MM-DD"` keys), each group in due order. A reminder due
  exactly at `now` is part of the digest; one due exactly at `now + days` is not. `now` defaults
  to the current time.
- `Agenda.add_local(id, message, text)` adds a reminder from an ISO 8601 string. Text that carries
  a UTC offset keeps it; text without one is read as local time in the agenda's timezone, which is
  set with `Agenda(tz=...)` and defaults to UTC.
