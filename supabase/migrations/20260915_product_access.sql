-- Run this in the Supabase SQL Editor. No application password belongs in this file.
create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text not null default 'customer' check (role in ('customer', 'admin')),
  plan_status text not null default 'inactive' check (plan_status in ('inactive', 'active', 'past_due', 'cancelled')),
  monthly_requests integer not null default 100 check (monthly_requests > 0),
  requests_used integer not null default 0 check (requests_used >= 0),
  free_requests_limit integer not null default 10 check (free_requests_limit >= 0),
  free_requests_used integer not null default 0 check (free_requests_used >= 0),
  usage_period date not null default date_trunc('month', now())::date,
  created_at timestamptz not null default now()
);

-- Safe when this migration is applied to a project that already has profiles.
alter table public.profiles
  add column if not exists free_requests_limit integer not null default 10,
  add column if not exists free_requests_used integer not null default 0;

alter table public.profiles enable row level security;
create policy "users read their own profile" on public.profiles for select using (auth.uid() = user_id);

create or replace function public.create_profile()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (user_id) values (new.id);
  return new;
end;
$$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users for each row execute procedure public.create_profile();

-- Accounts made before this migration need profiles too.
insert into public.profiles (user_id)
select id from auth.users
on conflict (user_id) do nothing;

-- Atomically consumes one request. New accounts receive ten free requests;
-- paid plans reset and consume their monthly allowance. Admins are unlimited.
create or replace function public.consume_screen_request()
returns boolean language plpgsql security definer set search_path = public as $$
declare p public.profiles%rowtype; this_period date := date_trunc('month', now())::date;
begin
  select * into p from public.profiles where user_id = auth.uid() for update;
  if not found then return false; end if;
  if p.role = 'admin' then return true; end if;
  if p.plan_status <> 'active' then
    if p.free_requests_used >= p.free_requests_limit then return false; end if;
    update public.profiles set free_requests_used = free_requests_used + 1 where user_id = auth.uid();
    return true;
  end if;
  if p.usage_period <> this_period then
    update public.profiles set requests_used = 1, usage_period = this_period where user_id = auth.uid();
    return true;
  end if;
  if p.requests_used >= p.monthly_requests then return false; end if;
  update public.profiles set requests_used = requests_used + 1 where user_id = auth.uid();
  return true;
end;
$$;
grant execute on function public.consume_screen_request() to authenticated;

-- After creating your own Auth user, promote it in the SQL Editor:
-- update public.profiles set role = 'admin', plan_status = 'active' where user_id = 'YOUR_AUTH_USER_UUID';
