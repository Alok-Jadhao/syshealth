#!/bin/bash
set -e

KEY=~/syshealth-key.pem
SERVER=13.61.11.18
AGENTS="16.171.200.36 13.51.45.7 16.16.121.89 13.48.195.203"

SSH="ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=8"
SCP="scp -i $KEY -o StrictHostKeyChecking=no"

deploy_agents() {
  echo ">> Deploying agent files..."
  for IP in $AGENTS; do
    $SCP syshealth.py analyzer.py collector.py reporter.py ubuntu@$IP:~/ &
  done
  wait

  echo ">> Restarting agents..."
  for IP in $AGENTS; do
    $SSH ubuntu@$IP bash << 'ENDSSH' &
pkill -f syshealth.py 2>/dev/null || true
nohup python3 syshealth.py >> agent.log 2>&1 &
echo "$(hostname): agent restarted (PID $!)"
ENDSSH
  done
  wait
  echo ">> Agents done."
}

deploy_server() {
  echo ">> Deploying server files..."
  $SCP server.py ubuntu@$SERVER:~/
  $SCP templates/index.html ubuntu@$SERVER:~/templates/index.html

  echo ">> Restarting server..."
  $SSH ubuntu@$SERVER bash << 'ENDSSH'
pkill -f server.py 2>/dev/null || true
nohup python3 server.py > server.log 2>&1 &
echo "Server restarted (PID $!)"
ENDSSH
  echo ">> Server done. Dashboard: http://$SERVER:5000"
}

case "${1:-all}" in
  agents) deploy_agents ;;
  server) deploy_server ;;
  all)    deploy_agents; deploy_server ;;
  *)      echo "Usage: ./deploy.sh [all|agents|server]"; exit 1 ;;
esac
