# Deploying the Fragnetic landing page

`index.html` is fully self-contained (inline CSS, no build step). Host it free
in ~5 minutes and you get a real URL to put in Lemon Squeezy and everywhere else.

## Fastest option — Netlify Drop (no account gymnastics)
1. Go to **app.netlify.com/drop**
2. Drag the whole `site/` folder onto the page
3. You instantly get a URL like `fragnetic-xyz.netlify.app` — use that
4. (Optional) claim it to a free Netlify account to keep it + add a custom domain

## Alternative — Cloudflare Pages / GitHub Pages
- **Cloudflare Pages**: create a project, connect a repo or direct-upload `site/`.
- **GitHub Pages**: push `site/` to a PUBLIC repo, enable Pages in repo settings.
  (Your main `fragnetic` repo is private — either make a small separate public
  repo just for the site, or use Netlify/Cloudflare which don't require public.)

## Custom domain (optional, ~$10/yr)
Grab `fragnetic.app` / `fragnetic.gg` from any registrar and point it at your
Netlify/Cloudflare site. A real domain also helps Lemon Squeezy approve your
store faster and looks far more legit to buyers than a `*.netlify.app` URL.

## Before you go live — 3 TODOs in index.html
1. **Download button** (`#get` section + nav): point `href="#"` at your actual
   download link or Lemon Squeezy checkout URL.
2. **Support email** (footer): replace `TODO@example.com`.
3. **Legal links**: the footer/disclaimer link to `EULA.html`, `PRIVACY.html`,
   `REFUND.html` — those don't exist yet (the docs are currently Markdown).
   Either convert the `.md` files to simple `.html` pages in this folder, or
   change the links to point at hosted copies. (Ask me to generate the HTML
   versions — quick.)

## What to put in Lemon Squeezy's "website URL" field right now
Once deployed, use your Netlify/Cloudflare/custom URL. Until then,
`https://fragnetic.lemonsqueezy.com` (your LS store page) is a fine answer.
