#!/bin/sh
#
# Serve Apple's app-site-association file, if this deployment has an iOS app id for it.
#
# WHY THIS EXISTS. The iOS app's sign-in sheet waits for an https handoff link on this
# host (`/app/signed-in`), and Apple only permits that for an app carrying an
# associated-domains entitlement for the host — which it verifies by fetching this file.
# That is what stops any other app on the phone from receiving a Motet sign-in, which a
# custom URL scheme cannot do.
#
# THE SERVICE IS `webcredentials`, NOT `applinks`. An https callback to
# `ASWebAuthenticationSession` is not a universal link — it is verified through the
# shared-web-credentials service, and a session asked for one on a domain the app claims
# only under `applinks` refuses to start. `applinks` is also deliberately absent: claiming
# `/app/signed-in` would route every tap on that URL, anywhere on the phone, into an app
# that has no handler for it. `webcredentials` claims no URL at all, so nothing about this
# web app leaves the browser.
#
# It is written here rather than committed for `config.js`'s reason, twice over: the file
# names the Apple team id, and this repo is public. MOTET_IOS_APP_ID is
# `<TEAMID>.<bundle id>`, set by the service definition in the private infrastructure repo.
#
# Unset means the file is not served at all, and the API's MOTET_IOS_APP_LINK stays off
# beside it: an app told to wait for a link this host does not advertise would sit in a
# sheet that never closes.
set -eu

WELL_KNOWN=/usr/share/nginx/html/.well-known
AASA="${WELL_KNOWN}/apple-app-site-association"
APP_ID="${MOTET_IOS_APP_ID:-}"

if [ -z "$APP_ID" ]; then
  rm -f "$AASA"
  echo "motet-web: MOTET_IOS_APP_ID is unset; not serving an app-site-association file." >&2
  exit 0
fi

# `<10-char team>.<bundle id>`, and nothing else: the value is interpolated into JSON, and
# a hostname-shaped typo here fails Apple's fetch silently weeks later rather than now.
case "$APP_ID" in
  *[!A-Za-z0-9.-]*)
    echo "motet-web: refusing MOTET_IOS_APP_ID with characters outside [A-Za-z0-9.-]." >&2
    exit 1
    ;;
  *.*.*) ;;
  *)
    echo "motet-web: MOTET_IOS_APP_ID must be <TEAMID>.<bundle id>, e.g. ABCDE12345.com.example.app." >&2
    exit 1
    ;;
esac

mkdir -p "$WELL_KNOWN"

cat > "$AASA" <<JSON
{
  "webcredentials": {
    "apps": ["${APP_ID}"]
  }
}
JSON

echo "motet-web: serving /.well-known/apple-app-site-association for ${APP_ID}"
