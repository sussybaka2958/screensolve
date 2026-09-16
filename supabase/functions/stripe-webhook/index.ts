import { createClient } from "npm:@supabase/supabase-js@2";

const PLAN_LIMITS: Record<string, number> = { starter: 100, plus: 500, power: 2000 };
const subtle = crypto.subtle;
const encoder = new TextEncoder();

const equals = (a: string, b: string) => {
  if (a.length !== b.length) return false;
  let diff = 0; for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
};
async function validSignature(payload: string, header: string | null) {
  if (!header) return false;
  const fields = Object.fromEntries(header.split(",").map((part) => part.split("=", 2)));
  const timestamp = Number(fields.t); const signature = fields.v1;
  if (!timestamp || !signature || Math.abs(Date.now() / 1000 - timestamp) > 300) return false;
  const key = await subtle.importKey("raw", encoder.encode(Deno.env.get("STRIPE_WEBHOOK_SECRET")!), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const bytes = await subtle.sign("HMAC", key, encoder.encode(`${timestamp}.${payload}`));
  const expected = [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return equals(expected, signature);
}

Deno.serve(async (request) => {
  const raw = await request.text();
  if (!await validSignature(raw, request.headers.get("stripe-signature"))) return new Response("Invalid Stripe signature", { status: 400 });
  const event = JSON.parse(raw); const object = event.data?.object;
  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
  if (event.type.startsWith("customer.subscription.")) {
    const plan = object.metadata?.plan;
    const active = ["active", "trialing"].includes(object.status);
    const status = active ? "active" : object.status === "past_due" ? "past_due" : "cancelled";
    await admin.from("profiles").update({ plan_id: plan || null, plan_status: status, monthly_requests: PLAN_LIMITS[plan] ?? 100, stripe_subscription_id: object.id }).eq("stripe_customer_id", object.customer);
  } else if (event.type === "invoice.payment_failed") {
    await admin.from("profiles").update({ plan_status: "past_due" }).eq("stripe_customer_id", object.customer);
  }
  return new Response("ok", { status: 200 });
});
