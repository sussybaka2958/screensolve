import Stripe from "npm:stripe@17.7.0";
import { createClient } from "npm:@supabase/supabase-js@2";

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "Content-Type": "application/json" } });
const plans: Record<string, string | undefined> = {
  starter: Deno.env.get("STRIPE_PRICE_STARTER"),
  plus: Deno.env.get("STRIPE_PRICE_PLUS"),
  power: Deno.env.get("STRIPE_PRICE_POWER"),
};

Deno.serve(async (request) => {
  if (request.method !== "POST") return json({ error: "POST required" }, 405);
  const authorization = request.headers.get("Authorization");
  if (!authorization?.startsWith("Bearer ")) return json({ error: "Sign in required" }, 401);
  const userClient = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!, { global: { headers: { Authorization: authorization } } });
  const { data: { user } } = await userClient.auth.getUser();
  if (!user?.email) return json({ error: "Invalid session" }, 401);
  let plan: string;
  try { plan = (await request.json()).plan; } catch { return json({ error: "Invalid request" }, 400); }
  const price = plans[plan];
  if (!price) return json({ error: "Unknown plan" }, 400);

  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const { data: profile } = await admin.from("profiles").select("stripe_customer_id").eq("user_id", user.id).single();
  const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!);
  const customer = profile?.stripe_customer_id
    ? profile.stripe_customer_id
    : (await stripe.customers.create({ email: user.email, metadata: { supabase_user_id: user.id } })).id;
  if (!profile?.stripe_customer_id) await admin.from("profiles").update({ stripe_customer_id: customer }).eq("user_id", user.id);

  const origin = Deno.env.get("BILLING_RETURN_URL")!;
  const session = await stripe.checkout.sessions.create({
    mode: "subscription", customer, line_items: [{ price, quantity: 1 }],
    success_url: `${origin}?billing=success`, cancel_url: `${origin}?billing=cancelled`,
    metadata: { supabase_user_id: user.id, plan }, subscription_data: { metadata: { supabase_user_id: user.id, plan } },
  });
  return json({ url: session.url });
});
