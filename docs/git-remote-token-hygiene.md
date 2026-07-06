# Git remote token hygiene

**Convention: never bake a credential into a git remote URL.** Clone clean and let
the credential helper supply auth.

## The problem

Several checkouts across our orgs were cloned with a token embedded in the remote:

```
https://x-access-token:ghs_XXXXXXXX@github.com/<owner>/<repo>.git
```

This is bad on two counts:

1. **Secret-at-rest.** The token sits in plaintext in `.git/config` — readable by
   anything that can read the working copy, and it leaks into backups and tarballs.
2. **It goes stale and silently breaks the checkout.** `ghs_` is a GitHub App
   *installation* token; it expires ~1 hour after it's minted. Once expired the
   checkout can no longer `fetch` or `push`, because git uses the credential
   embedded in the URL verbatim and **never falls back to a credential helper**
   when the URL already carries userinfo. You get:

   ```
   remote: Invalid username or token. Password authentication is not supported.
   fatal: Authentication failed for 'https://github.com/<owner>/<repo>.git/'
   ```

## The fix

Use a **clean** remote URL and let git's credential helper serve auth per
operation:

```
git remote set-url origin https://github.com/<owner>/<repo>.git
```

With `gh` configured as the helper (as on our boxes:
`credential.https://github.com.helper = !gh auth git-credential`), git calls
`gh auth git-credential`, which returns a live token for the active `gh` account.
A clean URL therefore keeps working indefinitely — the helper handles expiry and
rotation.

> Auth is selected by the active `gh` account, which is chosen per box via
> `GH_CONFIG_DIR` (e.g. `~/.config/gh-<org>`). The remote URL should never encode
> which token to use.

## Remediation

`scripts/scrub-remote-tokens.sh` rewrites any tokened `https://…@github.com/…`
remote to its clean form across every git checkout under a root (default
`~/repos`).

```bash
# preview across all checkouts
scripts/scrub-remote-tokens.sh --dry-run

# apply
scripts/scrub-remote-tokens.sh

# scope to specific roots
scripts/scrub-remote-tokens.sh ~/repos/cashbucket ~/repos/rolliq
```

SSH remotes (`git@github.com:…`) and already-clean https remotes are left
untouched. The script only edits local `.git/config`; it makes no network calls
and pushes nothing.

Run it on each box that has checkouts (it's a per-machine change — `.git/config`
is not tracked, so there is no PR that fixes an existing clone; this repo ships
the tool and the convention).
