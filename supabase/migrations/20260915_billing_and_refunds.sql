-- Run after 20260915_product_access.sql. This gives Stripe—not the desktop app—
-- authority over paid access.
alter table public.profiles
  add column if not exists plan_id text,
  add column if not exists stripe_customer_id text unique,
  add column if not exists stripe_subscription_id text unique;

-- Returns one request to the current caller only. It is used solely when the
-- paid provider fails after consume_screen_request reserved a slot.
create or replace function public.refund_screen_request()
returns boolean language plpgsql security definer set search_path = public as $$
declare p public.profiles%rowtype; this_period date := date_trunc('month', now())::date;
begin
  select * into p from public.profiles where user_id = auth.uid() for update;
  if not found or p.role = 'admin' or p.usage_period <> this_period or p.requests_used <= 0 then
    return false;
  end if;
  update public.profiles set requests_used = requests_used - 1 where user_id = auth.uid();
  return true;
end;
$$;
grant execute on function public.refund_screen_request() to authenticated;
