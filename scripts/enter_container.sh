#!/bin/bash
# Same commands as indomitus-rover-core's scripts/enter_container.sh, from the GS:
#   gs    - shell in this repo's container (indomitus_ground_station)
#   rover - rover-core's script, always with -n: the GS reaches the rover through
#           the mast link, so it must not switch its Wi-Fi to the rover hotspot.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
ROVER_CORE_DIR="${ROVER_CORE_DIR:-$(dirname "$REPO_ROOT")/indomitus-rover-core}"

success() { echo -e "\e[32m[SUCCESS]\e[0m $1"; }
error()   { echo -e "\e[31m[ERROR]\e[0m $1"; exit 1; }
step()    { echo -e "\n\e[33m>>> $1\e[0m"; }

show_main_help() {
  cat << EOF
Usage: $0 {gs|rover} [OPTIONS]

Opens a shell in the ground-station or the rover container.

Commands:
  gs      indomitus_ground_station on this machine (started if needed)
  rover   rover_prod on the Jetson, via rover-core's enter_container.sh -n
          (rover-core: \$ROVER_CORE_DIR, default ${ROVER_CORE_DIR})

Per command: $0 gs -h
             $0 rover -h

Examples:
  $0 gs             # GS container
  $0 rover          # rover container
  $0 rover core     # tmux session
  $0 rover arm -r   # read-only
EOF
}

run_gs() {
  local CONTAINER_NAME="indomitus_ground_station"
  local COMPOSE_FILE="docker/docker-compose.gs.yaml"

  show_gs_help() {
    cat << EOF
Usage: $0 gs [OPTIONS]

Shell in the GS container (started if needed).

Options:
  --name NAME      Container (${CONTAINER_NAME})
  --compose FILE   Compose file used if it does not exist,
                   from repo root (${COMPOSE_FILE})
  -h, --help       Show this help
EOF
  }

  while [[ "$#" -gt 0 ]]; do
    case $1 in
      --name)    [[ "$#" -ge 2 ]] || error "$1 requires an argument."; CONTAINER_NAME="$2"; shift 2;;
      --compose) [[ "$#" -ge 2 ]] || error "$1 requires an argument."; COMPOSE_FILE="$2"; shift 2;;
      -h|--help) show_gs_help; exit 0;;
      *)         error "Unknown option: $1\nRun '$0 gs --help' for usage." ;;
    esac
  done

  docker info > /dev/null 2>&1 || error "Docker is not running."

  step "Checking Container State (${CONTAINER_NAME})..."
  local CONTAINER_STATUS
  CONTAINER_STATUS=$(docker inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || true)

  if [ "$CONTAINER_STATUS" = "running" ]; then
    success "Container '${CONTAINER_NAME}' is already running."
  elif [ "$CONTAINER_STATUS" = "exited" ] || [ "$CONTAINER_STATUS" = "created" ]; then
    echo "Container '${CONTAINER_NAME}' is stopped. Starting it..."
    docker start "$CONTAINER_NAME" > /dev/null
    success "Container started."
  elif [ -z "$CONTAINER_STATUS" ]; then
    [ -f "$REPO_ROOT/$COMPOSE_FILE" ] || error "Compose file not found: $COMPOSE_FILE"
    echo "Container '${CONTAINER_NAME}' does not exist. Bringing it up (${COMPOSE_FILE})..."
    docker compose -f "$REPO_ROOT/$COMPOSE_FILE" --project-directory "$REPO_ROOT" up -d
    success "Container created and started."
  else
    error "Container '${CONTAINER_NAME}' is in an unexpected state: '${CONTAINER_STATUS}'. Aborting."
  fi

  step "Opening Interactive Terminal..."
  docker exec -it "$CONTAINER_NAME" bash
  echo -e "\n\e[32m[DONE]\e[0m Session closed."
}

run_rover() {
  local TARGET="$ROVER_CORE_DIR/scripts/enter_container.sh"
  [ -x "$TARGET" ] || error "rover-core script not found: $TARGET\nClone indomitus-rover-core next to this repo or set ROVER_CORE_DIR."
  exec "$TARGET" rover "$@" -n
}

if [ $# -eq 0 ]; then
  show_main_help
  exit 1
fi

COMMAND=$1
shift

case "$COMMAND" in
  gs)        run_gs "$@" ;;
  rover)     run_rover "$@" ;;
  -h|--help) show_main_help; exit 0 ;;
  *)         error "Unknown command: '$COMMAND'. Must be 'gs' or 'rover'.\nRun '$0 --help' for usage." ;;
esac
