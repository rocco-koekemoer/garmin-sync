-- Run once in the Supabase SQL editor to create the table coach.py saves to.
create table if not exists coach_reports (
  report_date date primary key,
  model       text,
  report      text not null,
  created_at  timestamptz default now()
);

-- Keep it private like your other tables: no public access via the anon key.
alter table coach_reports enable row level security;
