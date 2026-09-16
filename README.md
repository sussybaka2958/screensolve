# ScreenSolve

ScreenSolve is a Windows desktop assistant: press `Ctrl + Shift + Space`, capture the screen, and receive a concise Gemini-powered explanation in a premium, scrollable overlay.

The overlay is draggable by its header only. The answer area is deliberately interactive: you can select text and scroll it without accidentally moving the window.

## One-click download for people using the app

1. On GitHub, open **Actions** and run the build workflow (or build locally below).
2. Create a GitHub **Release** and upload `dist/ScreenSolve.exe`.
3. Upload `dist/ScreenSolve-windows-x64.zip`. Users extract it and double-click `ScreenSolve.exe`, keeping `supabase_config.json` in the same folder. There is no Python or terminal setup for them, and that JSON contains only public Supabase configuration—not the Gemini key.

Windows may show a SmartScreen warning until the app is code-signed. Use **More info → Run anyway** only for your own unsigned release. For a public app, purchase a code-signing certificate before distributing it widely.

## Owner setup: accounts, plans, and one shared Gemini key

ScreenSolve has a real email/password flow. The customer app never contains the Gemini key. Instead, it sends a signed-in customer's screenshot to a protected Supabase Edge Function. That function checks the customer’s plan and uses your one server-side Gemini key.

1. In your [Supabase dashboard](https://supabase.com/dashboard), open the project whose URL is already in `supabase_config.example.json`.
2. Under **Authentication → Providers → Email**, enable Email. Keep confirmation enabled for a production app.
3. Open **SQL Editor**, paste and run `supabase/migrations/20260915_product_access.sql`.
4. In **Project Settings → API**, copy the publishable key (or legacy anon key). Copy `supabase_config.example.json` to `supabase_config.json` and paste it there. A publishable/anon key is intended to ship with the app; never paste a `service_role`/secret key there.
5. Open **Edge Functions → Secrets**, create a secret named `GEMINI_API_KEY`, and paste your Gemini key there. This is the only place the key goes.
6. Create `ask-screen` under **Edge Functions**, paste the contents of `supabase/functions/ask-screen/index.ts`, and deploy it. Ensure JWT verification is enabled.
7. Make your own account in the app. In the SQL Editor, use the final commented `update` command in the migration with your Auth user UUID to promote it to `admin`. Your normal sign-in now opens the Developer Console; no developer password is stored in the app.

The Edge Function reads the Gemini key from Supabase Secrets, and authenticated users call it with their own session JWT. Supabase documents this server-side secret pattern in its [Edge Function secrets guide](https://supabase.com/docs/guides/functions/secrets) and recommends authenticated function calls for signed-in users in its [function security guide](https://supabase.com/docs/guides/functions/auth).

## Gemini API key setup (owner only)

You need one Gemini API key for the whole product. Customers do not see, paste, or possess it.

1. Open [Google AI Studio](https://aistudio.google.com/apikey) and choose **Create API key**.
2. Copy the key.
3. Add it as the `GEMINI_API_KEY` Edge Function secret in Supabase, as described above.
4. Do not put it in `ScreenSolve.pyw`, `supabase_config.json`, GitHub Actions secrets that are printed in logs, or customer devices.

Never paste an API key into source code, GitHub, Discord, or a public issue. If it is exposed, revoke it immediately and create another. The developer password supplied in chat should also be changed before you use it anywhere, because it is no longer private.

## Charging customers

New accounts are inactive, so they cannot use paid Gemini requests. The included Stripe functions automate activation, plan changes, failed payments, and cancellation. The Gemini API key remains only in a Supabase Edge Function secret.

### One-time owner setup

1. In Stripe Dashboard, enable **Test mode**. Create three recurring monthly prices: Starter ($4.99), Plus ($9.99), and Power ($19.99). Copy each `price_...` ID.
2. In Supabase SQL Editor, run `supabase/migrations/20260915_billing_and_refunds.sql` after the original migration. This also adds the request-refund function.
3. In Supabase **Edge Functions → Secrets**, add these values:
   - `STRIPE_SECRET_KEY`: your `sk_test_...` Stripe key (later replace with `sk_live_...`).
   - `STRIPE_WEBHOOK_SECRET`: add this after step 7.
   - `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_PLUS`, `STRIPE_PRICE_POWER`: the three `price_...` IDs.
   - `BILLING_RETURN_URL`: a real public HTTPS page you control, such as `https://your-domain.com/billing`.
   - `SUPABASE_SERVICE_ROLE_KEY`: Project Settings → API → service-role/secret key. This secret belongs only in Supabase; never put it in the EXE.
4. Deploy `ask-screen` again from `supabase/functions/ask-screen/index.ts`. This is required for refunds on unsuccessful answers.
5. Create and deploy these functions with JWT verification **enabled**: `create-checkout-session` and `create-portal-session`.
6. Create and deploy `stripe-webhook` with JWT verification **disabled**. Stripe does not have a Supabase user JWT; the function verifies Stripe's signed webhook instead.
7. In Stripe Dashboard → Developers → Webhooks, add endpoint `https://YOUR_PROJECT_REF.supabase.co/functions/v1/stripe-webhook`. Subscribe it to `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, and `invoice.payment_failed`. Copy the `whsec_...` signing secret into the Supabase `STRIPE_WEBHOOK_SECRET` secret.
8. In Stripe Dashboard → Settings → Billing → Customer portal, turn on cancellation, plan changes, payment-method updates, and invoice history.
9. Build a new EXE, create a fresh test account, select a plan in Settings, and pay with Stripe's test card `4242 4242 4242 4242`. Confirm its `profiles` row changes to `active` with the correct allowance.

Do not activate paid plans from the desktop app or from an unverified browser-return page. Only the verified Stripe webhook changes paid access.

## Build the Windows executable

Install Python 3.11+ on the build machine, then right-click `build.ps1` and select **Run with PowerShell**, or run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build.ps1
```

The build creates `dist/ScreenSolve.exe`. Test this executable on a second Windows account or clean virtual machine before publishing it.

The build now adds Windows version metadata. To sign a release, install the Windows SDK `signtool`, put the certificate file path in `SCREENSOLVE_SIGN_CERT`, then run `build.ps1`:

```powershell
$env:SCREENSOLVE_SIGN_CERT = 'C:\path\to\your-code-signing-certificate.pfx'
.\build.ps1
```

The certificate password is deliberately not stored in this project. A code-signing certificate is necessary to establish Windows reputation and reduce SmartScreen warnings; version metadata alone is not a signature.

## Development run

```powershell
py -m pip install -r requirements.txt
Copy-Item supabase_config.example.json supabase_config.json
notepad supabase_config.json
pyw ScreenSolve.pyw
```

## Privacy

When a user triggers ScreenSolve, it takes a whole-screen image and sends it directly to Google Gemini. The image can contain private information, so users should not trigger it when secrets, passwords, financial information, or private messages are visible. ScreenSolve does not operate continuously and does not upload a screen image until the hotkey is pressed.
