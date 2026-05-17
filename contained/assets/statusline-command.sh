#!/bin/sh
input=$(cat)
cwd=$(echo "$input" | jq -r '.cwd')
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // 0')
total_in=$(echo "$input" | jq -r '.context_window.total_input_tokens // 0')
win_size=$(echo "$input" | jq -r '.context_window.context_window_size // 200000')

# Format token counts as compact k values (integer k)
used_k=$(awk "BEGIN { printf \"%dk\", int($total_in / 1000 + 0.5) }")
size_k=$(awk "BEGIN { printf \"%dk\", int($win_size / 1000 + 0.5) }")

# Pick color for context segment: red >= 85, yellow >= 60, dim/gray otherwise
if [ "$used_pct" -ge 85 ] 2>/dev/null; then
    ctx_color="\033[02;31m"
elif [ "$used_pct" -ge 60 ] 2>/dev/null; then
    ctx_color="\033[02;33m"
else
    ctx_color="\033[02;37m"
fi

printf "\033[01;32m%s@%s\033[00m:\033[01;34m%s\033[00m" "$(whoami)" "$(hostname -s)" "$cwd"
printf "${ctx_color} | ctx %s%% (%s/%s)\033[00m" "$used_pct" "$used_k" "$size_k"
