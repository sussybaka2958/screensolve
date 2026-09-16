import Stripe from "npm:stripe@17.7.0";
import { createClient } from "npm:@supabase/supabase-js@2";

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "Content-Type": "application/json" } });
Deno.serve(async (request) => {
  if (request.method !== "POST") return json({ error: "POST required" }, 405);
  const authorization = request.headers.get("Authorization");
  if (!authorization?.startsWith("Bearer ")) return json({ error: "Sign in required" }, 401);
  const userClient = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!, { global: { headers: { Authorization: authorization } } });
  const { data: { user } } = await userClient.auth.getUser();
  if (!user) return json({ error: "Invalid session" }, 401);
  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  const { data: profile } = await admin.from("profiles").select("stripe_customer_id").eq("user_id", user.id).single();
  if (!profile?.stripe_customer_id) return json({ error: "You do not have a billing account yet." }, 400);
  const stripe = new Stripe(Deno.env.get("STRIPE_SECRET_KEY")!);
  const session = await stripe.billingPortal.sessions.create({ customer: profile.stripe_customer_id, return_url: Deno.env.get("BILLING_RETURN_URL")! });
  return json({ url: session.url });
});
