import { createClient } from "npm:@supabase/supabase-js@2";

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "Content-Type": "application/json" } });

Deno.serve(async (request) => {
  if (request.method !== "POST") return json({ error: "POST required" }, 405);
  const authorization = request.headers.get("Authorization");
  if (!authorization?.startsWith("Bearer ")) return json({ error: "Sign in required" }, 401);

  // This client is scoped to the caller's JWT; it cannot read another customer.
  const supabase = createClient(
    Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!,
    { global: { headers: { Authorization: authorization } } },
  );
  const { data: { user }, error: authError } = await supabase.auth.getUser();
  if (authError || !user) return json({ error: "Invalid session" }, 401);
  let image: string;
  try { image = (await request.json()).image; } catch { return json({ error: "Invalid request" }, 400); }
  if (!image || image.length > 10_000_000) return json({ error: "Screenshot is missing or too large." }, 400);

  // Reserve immediately before the paid provider call. A failed provider call is
  // refunded below, while concurrent requests cannot bypass a monthly limit.
  const { data: allowed, error: quotaError } = await supabase.rpc("consume_screen_request");
  if (quotaError || !allowed) return json({ error: "Your plan is inactive or its monthly allowance is used." }, 403);
  let refundable = true;
  const refund = async () => {
    if (!refundable) return;
    refundable = false;
    const { error } = await supabase.rpc("refund_screen_request");
    if (error) console.error("Could not refund request", error.message);
  };

  let google: Response;
  try {
    google = await fetch("https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": Deno.env.get("GEMINI_API_KEY")! },
      body: JSON.stringify({ contents: [{ parts: [
        { inline_data: { mime_type: "image/png", data: image } },
        { text: "Study this screenshot and help concisely. Explain errors and code clearly. Use plain text, short paragraphs and dashes; no markdown or LaTex." },
      ] }] }),
    });
  } catch (error) {
    console.error("Gemini network request failed", error);
    await refund();
    return json({ error: "The answer service is temporarily unavailable." }, 502);
  }
  if (!google.ok) {
    console.error("Gemini request failed", google.status);
    await refund();
    return json({ error: "The answer service is temporarily unavailable." }, 502);
  }
  let result: any;
  try {
    result = await google.json();
  } catch (error) {
    console.error("Gemini returned unreadable JSON", error);
    await refund();
    return json({ error: "The answer service is temporarily unavailable." }, 502);
  }
  const text = result?.candidates?.[0]?.content?.parts?.map((part: { text?: string }) => part.text ?? "").join("").trim();
  if (!text) {
    await refund();
    return json({ error: "The model returned no answer." }, 502);
  }
  refundable = false;
  return json({ text });
});
