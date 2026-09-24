#!/usr/bin/env bash
set -euo pipefail

# Installs a weekly cron job that builds AND publishes GB Power Weekly's
# recap report (the /gbpw page) every Monday, against the app container
# brought up by deploy/setup.sh. Not something setup.sh does itself --
# that script only gets the app running; this is the separate piece
# covered in README's "Scheduled report builds" note, now automated
# rather than hand-edited into crontab.
#
# Two steps are needed, not one -- `gbpw run` builds a report but does
# NOT publish it (see routes_gbpw.py, which skips any report where
# published=0), so a cron job that only ran `gbpw run` would silently
# build a report nobody ever sees on the live site. `gbpw publish` is
# the separate, explicit step that makes it visible.
#
# Idempotent -- re-running this replaces any previous copy of this exact
# job in the crontab (matched by its own comment marker below) rather
# than appending a duplicate.
#
# Usage: bash deploy/weekly-cron.sh [app_dir]
#   app_dir  defaults to ~/Mazao-Market-Intelligence (deploy/setup.sh's
#            own default clone location)

APP_DIR="${1:-$HOME/Mazao-Market-Intelligence}"
MARKER="# gbpw-weekly-report (managed by deploy/weekly-cron.sh)"
LOG_FILE="$HOME/gbpw-weekly.log"

CRON_LINE="0 6 * * 1 cd $APP_DIR && sudo docker compose exec -T gbpw sh -c 'D=\$(date -d yesterday +%F); gbpw run --week-ending \"\$D\" && gbpw publish --week-ending \"\$D\"' >> $LOG_FILE 2>&1 $MARKER"

echo "==> Installing weekly cron job (Mondays 06:00 UTC -- builds + publishes the just-completed week)"
( crontab -l 2>/dev/null | grep -vF "$MARKER" ; echo "$CRON_LINE" ) | crontab -

echo "==> Done. Current crontab:"
crontab -l
echo
echo "    Logs land in $LOG_FILE once the first Monday run happens."
echo "    To trigger one manually right now (e.g. to test it works):"
echo "    cd $APP_DIR && sudo docker compose exec gbpw sh -c 'D=\$(date -d yesterday +%F); gbpw run --week-ending \"\$D\" && gbpw publish --week-ending \"\$D\"'"
