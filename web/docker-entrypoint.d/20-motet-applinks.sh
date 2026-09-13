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

# One path, the handoff's. Apple reads `applinks` for universal links and for the https
# callback an ASWebAuthenticationSession waits on; nothing else about this host is claimed,
# so opening any other Motet URL still opens the browser.
cat > "$AASA" <<JSON
{
  "applinks": {
    "details": [
      { "appIDs": ["${APP_ID}"], "components": [{ "/": "/app/signed-in", "comment": "iOS sign-in handoff" }] }
    ]
  }
}
JSON

echo "motet-web: serving /.well-known/apple-app-site-association for ${APP_ID}"
