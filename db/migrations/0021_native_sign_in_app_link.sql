-- Which callback the iOS app that started this sign-in can actually receive.
--
-- Migration 0019 added `handoff_challenge`, which marks a sign-in the app started. This
-- records the second half of that: whether the app asked for — and this deployment agreed
-- to — an https handoff on the web app's own host, rather than the `motet://` scheme.
--
-- It has to be stored rather than re-derived at callback time, because the callback is made
-- by the *browser*, which knows nothing about the app's iOS version or its entitlement. The
-- API deciding from its own flag alone would hand back an https link to an app whose sign-in
-- sheet is watching for the scheme, and that sheet would never close.
ALTER TABLE oauth_states
    ADD COLUMN handoff_app_link boolean NOT NULL DEFAULT false;
