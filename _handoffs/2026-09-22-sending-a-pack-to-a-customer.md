# Sending someone a Content Pack

Attach the `.sspack` file to an email. That is the whole procedure.

## What they do

On the Mac running their gateway:

1. Open the gateway app → **Content Packs** → **Shelved**.
2. Under **Add a pack someone sent you**, choose the `.sspack` file.
3. Press **Add to shelf**.

It appears on their shelf immediately and Story Studio can download and install it.

Nothing else is needed — no key of yours, no configuration, and no internet beyond
their own Mac. The file carries its own name, description, kind and identity, and
it is signed on their machine with *their* gateway's own identity.

## What they need first

- The gateway app installed, with the quarantime flag cleared
  (`xattr -dr com.apple.quarantine "/Applications/gateway-kh.app"`, or right-click
  → **Open** once), and their **own** provider API keys entered under **Providers**.
- Their private shelf initialised once: **Content Packs** → **Setup** → set it up.
  This happens on their machine and creates their own signing identity. Your key is
  never involved and never shared.

## Notes

- Packs are limited to 50 MiB each; send several files if you have several packs.
- A pack whose identity belonged to the real Store is refused by design — that
  guard exists so a private shelf can never impersonate a Store product.
- Updates work the same way: send the newer `.sspack` and they add it. Story
  Studio keeps both versions available, exactly as it does for the packs already
  installed.
