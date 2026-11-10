#!/bin/bash
# Claude Code PreToolUse hook for every tool in this repo.
# Contract: https://code.claude.com/docs/en/hooks.md — exit 2 blocks the tool call and shows stderr to Claude.
# Hooks run in every permission mode, including bypass, so these rules hold when nothing else asks.
# .claude/settings.json sets "onFailure": "block" on this hook, so a crash or a timeout also blocks the call.
#
# Part 1, every session (ADR 0009, ADR 0011):
#   - no commit that skips the commit gate: a short-flag cluster holding n, any accepted abbreviation of
#     --no-verify, an override of core.hooksPath, an include or an alias (-c, --config-env, git config),
#     GIT_CONFIG_* variables, or a git subcommand this hook cannot read as a plain word
#   - no install that bypasses uv.lock, and no `uv run` without --locked or --frozen among its options
#   - no call to Alpaca's live trading or production broker hosts (names read from pinned alpaca-py)
#   - no Python, in a file or in a shell command, that opts out of paper trading
# Live-host, environment-switch, install and Python checks match a normalised copy of the command (see
# norm below) with commit and pull request message text left out, and a URL given to WebFetch is read by
# URL rules (see url_norm). A single grep, rg or git grep command is a search and is not matched.
# Part 2, unattended sessions (marked by unattended-mark.sh): read-only by default-deny.
#
# This is text matching, not a sandbox: it cannot see a value the shell assembles when the command runs,
# such as a name built from variables or brace expansion, or a payload decoded and piped to a shell.
# It sees only `git commit`, so a commit made another way (plumbing such as commit-tree and update-ref, or
# an alias already defined in git config) does not reach the commit gate checks.
# It looks for live hosts only in commands and Python files, so a live host written into another kind of
# file is not refused.
# It recognises $'...' quoting only in a command whose word is a fetching program, not inside `bash -c`
# or a Python one-liner.
# It does not watch the other files the commit gate depends on, such as .githooks/ or a global git config.
# It recognises only paper=False and paper=0 as the live setting of TradingClient.
# Fails closed: if this hook cannot evaluate a call, the call is blocked.
export LC_ALL=C
input=$(cat)
deny() { echo "guard: blocked. $1" >&2; exit 2; }
command -v jq >/dev/null 2>&1 || deny "jq is not installed, so tool calls in this repo cannot be checked."
jq -e 'type == "object"' >/dev/null 2>&1 <<<"$input" || deny "the hook input was empty or not a JSON object."

root="${CLAUDE_PROJECT_DIR:-$(jq -r '.cwd // empty' <<<"$input")}"
tool=$(jq -r '.tool_name // empty' <<<"$input") || deny "the hook input was not valid JSON."
sid=$(jq -r '.session_id // empty' <<<"$input")

# alpaca-py 0.45.0, alpaca.common.enums.BaseURL: TRADING_LIVE, TRADING_STREAM_LIVE, BROKER_PRODUCTION.
# Anchored on characters that cannot be part of a host name, so paper-api.alpaca.markets and
# broker-api.sandbox.alpaca.markets stay allowed and a bare host with no // is still caught.
# A trailing root dot (api.alpaca.markets.) names the same host.
LIVE_HOSTS='(^|[^a-z0-9.-])(api|broker-api)\.alpaca\.markets\.?([^a-z0-9.-]|$)'
# Alpaca's CLI and MCP server switch to live through these variables (alpaca-skills at the pinned commit:
# paper-trading-cli/SKILL.md, paper-trading-mcp/SKILL.md). Matches shell, JSON and YAML spellings once
# quotes are removed.
LIVE_ENV='alpaca_live_trade[[:space:]]*[:=][[:space:]]*(true|1|yes)|alpaca_paper_trade[[:space:]]*[:=][[:space:]]*(false|0|no)'
# alpaca-py TradingClient(paper=False), url_override and the live BaseURL members.
PY_LIVE='paper[[:space:]]*=[[:space:]]*false|paper[[:space:]]*=[[:space:]]*0([^0-9a-z_.]|$)|url_override[[:space:]]*=|trading_live|trading_stream_live|broker_production'
# Installs that do not come from uv.lock: pip, pip3, pip3.14, python -m pip, uv pip install, uv pip sync,
# with or without options before the subcommand, at the start of a command, a subshell or a substitution.
PIP_OPTS='([[:space:]]+-[^[:space:]]+)*'
PIP_INSTALL='(^|[;&|[:space:]/(`!])(pip[0-9.]*'"$PIP_OPTS"'[[:space:]]+install|python[0-9.]*'"$PIP_OPTS"'[[:space:]]+-[a-z]*m[[:space:]]*pip[0-9.]*'"$PIP_OPTS"'[[:space:]]+install|uv'"$PIP_OPTS"'[[:space:]]+pip'"$PIP_OPTS"'[[:space:]]+(install|sync))([[:space:]]|$)'
# Hosts an unattended session may fetch: a subset of the WebFetch allow-list in .claude/settings.json,
# covering what the landscape-refresh skill and the evidence-verifier read. scripts/test-hooks checks it.
UNATTENDED_HOSTS='github.com raw.githubusercontent.com nautilustrader.io docs.alpaca.markets code.claude.com platform.claude.com artificialanalysis.ai docs.astral.sh mise.jdx.dev'
# uv run options that take a value, global ones included, lower-cased (uv run --help, uv 0.13.7).
UV_VALUE_OPTS=' --extra --no-extra --group --no-group --only-group --no-editable-package --env-file --with
 --with-editable --with-requirements --package --python-platform --index --default-index --index-url
 --extra-index-url --find-links --index-strategy --keyring-provider --upgrade-package --upgrade-group
 --resolution --prerelease --prerelease-package --fork-strategy --exclude-newer --exclude-newer-package
 --no-sources-package --reinstall-package --link-mode --config-setting --config-settings-package
 --no-build-isolation-package --no-build-package --no-binary-package --cache-dir --refresh-package --python
 --color --allow-insecure-host --directory --project --config-file -w -i -f -p -c '
# A Bash command longer than this that holds a git commit is refused rather than parsed.
MAX_COMMIT_CMD=16384
# A single command with more words than this is refused rather than parsed: bash 3.2 arrays take time
# that grows with their length on every append and lookup.
MAX_WORDS=4096

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# norm TEXT: the copy of a shell command the live-host, environment and Python patterns are matched
# against. Bash removes quotes and joins backslash-newlines before it runs a command (bash(1), QUOTING),
# and host names are case-insensitive (RFC 3986, 3.2.2), so backslash-newline pairs, quote characters and
# the remaining backslashes are deleted, letters are lower-cased, a userinfo@ after // is removed (RFC 3986,
# 3.2.1), and line breaks and tabs become spaces so that a keyword argument split across lines is seen whole.
norm() {
  local s="$1"
  case "$s" in *$'\\\n'*) s=${s//$'\\\n'/} ;; esac
  s=$(printf '%s' "$s" | tr -d "\\\\\"'" | tr '[:upper:]' '[:lower:]' | tr '\n\r\t' '   ') || deny "the text could not be normalised."
  printf '%s' "$s" | sed -E 's#//[^/@[:space:]]*@#//#g'
}

# pdecode TEXT: TEXT with each %HH escape decoded (RFC 3986, 2.1). TEXT holds no backslash.
pdecode() {
  case "$1" in
    *%[0-9A-Fa-f][0-9A-Fa-f]*) printf '%b' "$(printf '%s' "$1" | sed -E 's/%([0-9A-Fa-f]{2})/\\x\1/g')" ;;
    *) printf '%s' "$1" ;;
  esac
}

# url_norm URL: the URL as a URL parser reads it (RFC 3986, 3.2): the user information before the last
# @ of the authority is dropped, then %HH escapes are decoded and letters lower-cased.
url_norm() {
  local u="$1" scheme rest auth
  case "$u" in
    *://*) scheme=${u%%://*}; rest=${u#*://}; auth=${rest%%[/?#]*}; rest=${rest#"$auth"}
           u="$scheme://${auth##*@}$rest" ;;
  esac
  lower "$(pdecode "$u")"
}

# split_words TEXT CALLBACK: split TEXT into shell words the way bash(1) does before running it (quotes
# removed, backslash escapes applied, backslash-newline joined), and call CALLBACK (git_cmd or search_cmd)
# once for each command, that is at each newline ; & | ( ) or backtick, with its words in the array W.
# X[i] is 1 when word i holds an unquoted or double-quoted expansion ($, backticks, globs, braces, ~), whose value this
# hook cannot know, and 2 when it holds $'...' quoting, whose escapes are not decoded. P[i] and E[i] are the
# offsets in TEXT where word i starts and ends. Here-documents (<<WORD, <<-WORD) are not split: a command
# that ends at a newline gets, for each body that follows, its offsets in HS and HE, HQ 1 when its delimiter
# was quoted (no expansion in the body), and HO the index in W of the word that followed the << operator
# (-1 when the operator was in an earlier command on that line). TEXT is read in chunks, and the arrays
# hold one command at a time, so the time taken grows linearly with the length of TEXT.
split_words() {
  local s="$1" cb="$2" n=${#1} i=0 o c c1 w="" have=0 ex=0 q="" ws=0 we=0 base=0 chunk="" clen=0
  local d dq strip k line rest cmp found bs run
  local sp=$' \t\n\'"\\$;&|()`<>*?[{~' dsp='"\$`'   # characters that end a run of plain text
  local -a pd ps pq po
  W=(); X=(); P=(); E=(); HS=(); HE=(); HQ=(); HO=()
  flush() {
    W+=("$w"); X+=("$ex"); P+=("$ws"); E+=("$we"); w=""; have=0; ex=0
    (( ${#W[@]} <= MAX_WORDS )) || deny "One command in that text has more than $MAX_WORDS words, which this hook does not parse. Split it up, or put long text in a file with the Write tool."
  }
  endcmd() {
    if (( ${#W[@]} || ${#HS[@]} )); then
      case "$cb" in git_cmd) git_cmd ;; search_cmd) search_cmd ;; esac
    fi
    W=(); X=(); P=(); E=(); HS=(); HE=(); HQ=(); HO=()
  }
  while (( i < n )); do
    if (( (o = i - base) >= clen - 1 )); then chunk=${s:i:1024}; base=$i; clen=${#chunk}; o=0; fi
    c=${chunk:o:1}; (( i++ ))
    case "$q" in
      "'") if [[ $c == "'" ]]; then q=""; else run=${chunk:o}; run=${run%%\'*}; w+=$run; (( i = base + o + ${#run} )); fi
           we=$i; continue ;;
      '"') case "$c" in
             '"') q="" ;;
             "\\") c1=${chunk:o+1:1}
                  case "$c1" in
                    '$'|'`'|'"'|"\\") w+=$c1; (( i++ )) ;;
                    $'\n') (( i++ )) ;;
                    *) w+=$c ;;
                  esac ;;
             '$'|'`') w+=$c; [[ $ex == 2 ]] || ex=1 ;;
             *) run=${chunk:o}; run=${run%%["$dsp"]*}; w+=$run; (( i = base + o + ${#run} )) ;;
           esac
           we=$i; continue ;;
    esac
    case "$c" in
      ' '|$'\t') (( have )) && flush ;;
      "'"|'"') (( have )) || { have=1; (( ws = i - 1 )); }
               q=$c; we=$i ;;
      "\\") c1=${chunk:o+1:1}; (( i++ ))
           if [[ $c1 != $'\n' && -n $c1 ]]; then
             (( have )) || { have=1; (( ws = i - 2 )); }
             w+=$c1; we=$i
           fi ;;
      '$') (( have )) || { have=1; (( ws = i - 1 )); }
           w+=$c; c1=${chunk:o+1:1}
           if [[ $c1 == "'" ]]; then q="'"; ex=2; (( i++ )); else [[ $ex == 2 ]] || ex=1; fi
           we=$i ;;
      $'\n') (( have )) && flush
             # here-document bodies start on the line after their operator
             k=0
             while [ "$k" -lt "${#pd[@]}" ]; do
               bs=$i; found=0
               while [ "$i" -lt "$n" ]; do
                 rest=${s:i:4096}; line=${rest%%$'\n'*}
                 while [ "${#line}" = "${#rest}" ] && [ $((i+${#rest})) -lt "$n" ]; do
                   rest=${s:i:$((${#rest}*2))}; line=${rest%%$'\n'*}
                 done
                 cmp=$line
                 if [ "${ps[k]}" = 1 ]; then while case "$cmp" in $'\t'*) true ;; *) false ;; esac; do cmp=${cmp#?}; done; fi
                 if [ "$cmp" = "${pd[k]}" ]; then HE+=("$i"); i=$((i+${#line}+1)); found=1; break; fi
                 i=$((i+${#line}+1))
               done
               [ "$i" -gt "$n" ] && i=$n
               [ "$found" = 1 ] || HE+=("$n")
               HS+=("$bs"); HQ+=("${pq[k]}"); HO+=("${po[k]}")
               k=$((k+1))
             done
             pd=(); ps=(); pq=(); po=(); clen=0; base=$i
             endcmd ;;
      ';'|'&'|'|'|'('|')'|'`') (( have )) && flush
                               k=0; while (( k < ${#po[@]} )); do po[k]=-1; (( k++ )); done
                               endcmd ;;
      '<') (( have )) && flush
           c1=${chunk:o+1:1}
           if [ "$c1" = '<' ] && [ "${s:i+1:1}" != '<' ]; then
             i=$((i+1)); strip=0
             [ "${s:i:1}" = '-' ] && { strip=1; i=$((i+1)); }
             while case "${s:i:1}" in ' '|$'\t') true ;; *) false ;; esac; do i=$((i+1)); done
             d=""; dq=0
             while [ "$i" -lt "$n" ]; do
               c=${s:i:1}
               case "$c" in
                 ' '|$'\t'|$'\n'|';'|'&'|'|'|'('|')'|'<'|'>') break ;;
                 "'"|'"') dq=1; i=$((i+1))
                          while [ "$i" -lt "$n" ] && [ "${s:i:1}" != "$c" ]; do d="$d${s:i:1}"; i=$((i+1)); done ;;
                 "\\") dq=1; i=$((i+1)); d="$d${s:i:1}" ;;
                 *) d="$d$c" ;;
               esac
               i=$((i+1))
             done
             pd+=("$d"); ps+=("$strip"); pq+=("$dq"); po+=("${#W[@]}"); clen=0; base=$i
           else
             case "$c1" in '&'|'>'|'|') i=$((i+1)) ;; esac
           fi ;;
      '>') (( have )) && flush
           case "${chunk:o+1:1}" in '&'|'>'|'|') (( i++ )) ;; esac ;;
      '*'|'?'|'['|'{'|'~') (( have )) || { have=1; (( ws = i - 1 )); }
                           w+=$c; [[ $ex == 2 ]] || ex=1; we=$i ;;
      *) (( have )) || { have=1; (( ws = i - 1 )); }
         run=${chunk:o}; run=${run%%["$sp"]*}; [ -n "$run" ] || run=$c   # always take at least c
         w+=$run; (( i = base + o + ${#run} )); we=$i ;;
    esac
  done
  (( have )) && flush
  endcmd
  return 0
}

# literal_subst VALUE: true when VALUE is "$(cat <<'EOF' ... EOF)", a quoted here-document whose body
# bash does not expand, so the value is plain text.
literal_subst() {
  local v="$1" d body
  # shellcheck disable=SC2016  # the literal text $(cat is matched
  case "$v" in '$(cat <<'*) v=${v#'$(cat <<'} ;; *) return 1 ;; esac
  v=${v#-}
  case "$v" in "'"*) d=${v#\'}; d=${d%%\'*}; v=${v#\'"$d"\'} ;; *) return 1 ;; esac
  [ -n "$d" ] || return 1
  case "$v" in $'\n'*) v=${v#?} ;; *) return 1 ;; esac
  case "$v" in
    *$'\n'"$d"$'\n)') body=${v%$'\n'"$d"$'\n)'} ;;
    *$'\n'"$d)") body=${v%$'\n'"$d)"} ;;
    *) return 1 ;;
  esac
  case $'\n'"$body"$'\n' in *$'\n'"$d"$'\n'*) return 1 ;; esac
  return 0
}

# Offsets in the Bash command of text that is only a message (git commit -m, gh pr --title/--body, a
# quoted here-document fed to git commit -F -). The live-host, environment, install, uv and Python checks
# do not look at it.
SA=(); SB=()

# git_gate TEXT DEPTH: deny a git command in TEXT that would skip the commit hook or change git config in
# a way that could, and record message offsets (at depth 0). Flag forms follow git-commit(1), git-config(1)
# and git(1) as shipped with git 2.54: -n is --no-verify, short flags cluster (-nm), and long options may be
# abbreviated to any unambiguous prefix (gitcli(7)). Words that hold whitespace and name git, and
# here-document bodies, are split again, which covers `bash -c`, `eval`, quoted substitutions and a body
# fed to a shell.
git_gate() {
  local text="$1" depth="$2"
  [ "$depth" -gt 8 ] && deny "That git command nests too deeply to check for a commit that skips the gate."
  split_words "$text" git_cmd
}
# git_cmd: the split_words callback for git_gate; reads TEXT and DEPTH from git_gate.
git_cmd() {
  local i j k n e w x name cl ch fstdin c0 mk ansi fetch
  local -a words exps wp wq hs he hq ho msg body
  words=("${W[@]}"); exps=("${X[@]}"); wp=("${P[@]}"); wq=("${E[@]}")
  hs=("${HS[@]}"); he=("${HE[@]}"); hq=("${HQ[@]}"); ho=("${HO[@]}")
  n=${#words[@]}
  msg=(); body=()
  # $'...' escapes are not decoded, so a URL or host written with them cannot be matched
  ansi=0; fetch=0
  for x in "${exps[@]}"; do [ "$x" = 2 ] && ansi=1; done
  if [ "$ansi" = 1 ]; then
    for w in "${words[@]}"; do
      case "${w##*/}" in curl|wget|websocat|wscat|nc|ncat|telnet|ssh|http|https|xh|aria2c) fetch=1 ;; esac
    done
    [ "$fetch" = 1 ] && deny "A command that fetches or connects uses \$'...' quoting, which this hook does not decode. Write the URL or host with plain quotes."
  fi
  # the command word, after any NAME=value assignments and the plain wrappers env (and its assignments),
  # command, and mise exec ... --: only text given to it counts as a message
  c0=0
  while :; do
    while [ "$c0" -lt "$n" ]; do
      case "${words[c0]}" in
        *=*) name=${words[c0]%%=*}; case "$name" in ''|[0-9]*|*[!A-Za-z0-9_]*) break ;; esac ;;
        *) break ;;
      esac
      c0=$((c0+1))
    done
    case "${words[c0]}" in
      env|command) c0=$((c0+1)) ;;
      mise)
        [ "${words[c0+1]}" = exec ] || break
        k=$((c0+2)); while [ "$k" -lt "$n" ] && [ "${words[k]}" != -- ]; do k=$((k+1)); done
        [ "$k" -lt "$n" ] || break
        c0=$((k+1)) ;;
      *) break ;;
    esac
  done
  # scripts/land: the review note and the name of the pull request body file
  case "${words[c0]}" in
    scripts/land|./scripts/land)
      k=$((c0+1))
      while [ "$k" -lt "$n" ]; do
        case "${words[k]}" in
          --reviewed|--body) msg[k+1]=1; k=$((k+1)) ;;
          --reviewed=*|--body=*) msg[k]=1 ;;
        esac
        k=$((k+1))
      done ;;
  esac
  i=0
  while [ "$i" -lt "$n" ]; do
    w=${words[i]}
    mk=0; [ "$i" = "$c0" ] && mk=1
    # gh pr create / gh pr edit: --title, -t, --body and -b carry message text
    case "$w" in
      gh|*/gh)
        if [ "$mk" = 1 ] && [ "${words[i+1]}" = pr ] && case "${words[i+2]}" in create|edit) true ;; *) false ;; esac; then
          k=$((i+3))
          while [ "$k" -lt "$n" ]; do
            case "${words[k]}" in
              --title|--body|-t|-b) msg[k+1]=1; k=$((k+1)) ;;
              --title=*|--body=*|-t?*|-b?*) msg[k]=1 ;;
            esac
            k=$((k+1))
          done
        fi
        i=$((i+1)); continue ;;
      [Gg][Ii][Tt]|*/[Gg][Ii][Tt]) ;;                         # macOS file names ignore case
      *) i=$((i+1)); continue ;;
    esac
    # git's own options, up to the subcommand
    j=$((i+1))
    while [ "$j" -lt "$n" ]; do
      w=${words[j]}; x=${exps[j]}
      case "$w" in
        -c|--config-env) gate_config "${words[j+1]}" "${exps[j+1]}"; j=$((j+2)) ;;
        --config-env=*) gate_config "${w#--config-env=}" "$x"; j=$((j+1)) ;;
        -c*) gate_config "${w#-c}" "$x"; j=$((j+1)) ;;
        -C|--git-dir|--work-tree|--namespace|--super-prefix|--attr-source|--list-cmds) j=$((j+2)) ;;
        -*) j=$((j+1)) ;;
        *) break ;;
      esac
    done
    if [ "$j" -lt "$n" ] && [ "${exps[j]}" != 0 ]; then
      deny "The git subcommand '${words[j]}' is not a plain word, so this hook cannot tell what it runs. Spell it out."
    fi
    # an unquoted $(...) among git's options ends the command here, so the subcommand cannot be found
    if [ "$j" -ge "$n" ] && [ "$j" -gt $((i+1)) ] && [ "${exps[n-1]}" != 0 ]; then
      deny "A git option holds an unquoted substitution, so this hook cannot find the subcommand. Quote it: \"\$(...)\"."
    fi
    e=$n
    case "${words[j]}" in
      [Cc][Oo][Nn][Ff][Ii][Gg]) gate_git_config $((j+1)) "$e" ;;
      log)                                                  # a history search: its pattern is text
        k=$((j+1))
        while [ "$k" -lt "$n" ]; do
          case "${words[k]}" in
            --grep|-S|-G) msg[k+1]=$mk; k=$((k+1)) ;;
            --grep=*|-S?*|-G?*) msg[k]=$mk ;;
          esac
          k=$((k+1))
        done ;;
      [Cc][Oo][Mm][Mm][Ii][Tt])
        grep -qiE 'git_config' <<<"$text" \
          && deny "Commits go through .githooks/pre-commit; do not set GIT_CONFIG_* variables on a commit."
        j=$((j+1)); fstdin=0
        while [ "$j" -lt "$e" ]; do
          w=${words[j]}; x=${exps[j]}
          [ "$w" = "--" ] && break
          [ "$x" != 0 ] && deny "A git commit option holds an expansion this hook cannot check. Spell the flags out, and give a message as a file with git commit -F <file>."
          case "$w" in
            --*)
              name=${w%%=*}
              case "$NO_VERIFY" in "$name"*) [ "${#name}" -ge 6 ] && deny_skip ;; esac
              if [ "${#name}" -ge 3 ]; then
                for k in --message --file --author --date --reuse-message --reedit-message --fixup --squash \
                         --template --cleanup --trailer --pathspec-from-file; do
                  case "$k" in
                    "$name"*)
                      if [ "$name" = "$w" ]; then
                        [ "$k" = --message ] && msg[j+1]=$mk
                        [ "$k" = --file ] && [ "${#name}" -ge 5 ] && [ "${words[j+1]}" = - ] && fstdin=$mk
                        j=$((j+1))
                      else
                        [ "$k" = --message ] && msg[j]=$mk
                        [ "$k" = --file ] && [ "${#name}" -ge 5 ] && [ "${w#*=}" = - ] && fstdin=$mk
                      fi
                      break ;;
                  esac
                done
              fi ;;
            -?*)
              cl=${w#-}; k=0
              while [ "$k" -lt "${#cl}" ]; do
                ch=${cl:k:1}; k=$((k+1))
                case "$ch" in
                  n) deny_skip ;;
                  m|F|C|c|t)                                               # takes a value
                    if [ "$k" -ge "${#cl}" ]; then
                      [ "$ch" = m ] && msg[j+1]=$mk
                      [ "$ch" = F ] && [ "${words[j+1]}" = - ] && fstdin=$mk
                      j=$((j+1))
                    else
                      [ "$ch" = m ] && msg[j]=$mk
                      [ "$ch" = F ] && [ "${cl:k}" = - ] && fstdin=$mk
                    fi
                    break ;;
                  S|u) break ;;                                            # optional attached value
                  [A-Za-z0-9]) ;;
                  *) deny "A git commit short-option cluster holds '$ch', which this hook cannot check. If that text is part of a message, put the message in a file and use git commit -F <file>." ;;
                esac
              done ;;
          esac
          j=$((j+1))
        done
        # a here-document fed to git commit -F - is the message
        if [ "$fstdin" = 1 ]; then
          k=0
          while [ "$k" -lt "${#hs[@]}" ]; do
            if [ "${ho[k]}" -gt "$i" ] && [ "${ho[k]}" -le "$e" ]; then
              case "${text:${hs[k]}:$((he[k]-hs[k]))}" in            # bash expands $ and ` in an unquoted one
                *'$'*|*'`'*) [ "${hq[k]}" = 1 ] && body[k]=1 ;;
                *) body[k]=1 ;;
              esac
            fi
            k=$((k+1))
          done
        fi ;;
    esac
    i=$((i+1))
  done
  # words that run text of their own: $'...' around git, and words with whitespace that name git
  i=0
  while [ "$i" -lt "$n" ]; do
    w=${words[i]}; x=${exps[i]}
    if [ "${msg[i]:-0}" = 1 ] && { [ "$x" = 0 ] || literal_subst "$w"; }; then
      [ "$depth" = 0 ] && { SA+=("${wp[i]}"); SB+=("${wq[i]}"); }
      i=$((i+1)); continue
    fi
    case "$w" in
      *[Gg][Ii][Tt]*)
        [ "$x" = 2 ] && deny "A word quotes git text with \$'...', which this hook does not decode. Use plain quotes."
        case "$w" in *[[:space:]]*) git_gate "$w" $((depth+1)) ;; esac ;;
    esac
    i=$((i+1))
  done
  k=0
  while [ "$k" -lt "${#hs[@]}" ]; do
    if [ "${body[k]:-0}" = 1 ]; then
      [ "$depth" = 0 ] && { SA+=("${hs[k]}"); SB+=("${he[k]}"); }
    else
      case "${text:${hs[k]}:$((he[k]-hs[k]))}" in
        *[Gg][Ii][Tt]*) git_gate "${text:${hs[k]}:$((he[k]-hs[k]))}" $((depth+1)) ;;
      esac
    fi
    k=$((k+1))
  done
}
NO_VERIFY=--no-verify
deny_skip() { deny "Commits go through .githooks/pre-commit; do not pass --no-verify or -n. Fix what the gate reports. If -n is text in a message, put the message in a file and use git commit -F <file>."; }
# gate_config VALUE EXPANDS: VALUE is the argument of git -c or --config-env, key=value or key=ENVVAR.
gate_config() {
  local v
  [ "$2" != 0 ] && deny "A git -c or --config-env value holds an expansion this hook cannot check."
  v=$(lower "$1")
  case "$v" in
    *hookspath*|*include*) deny "Commits go through .githooks/pre-commit; do not override core.hooksPath or add a config include." ;;
    alias.*) deny "A git alias given on the command line can run any command, including a commit that skips the gate. Run the command itself." ;;
  esac
}
# gate_git_config FROM TO: the words of a git config command (git-config(1), both the subcommand form
# `git config set` and the older option form). The commit gate is the local core.hooksPath, so deny one
# that sets or unsets core.hooksPath, removes or renames the core section, sets a key under alias. or
# include (include.*, includeIf.*), renames a section to one of those, or opens the config in an editor.
# Reading (--get*, --list, get, list, or a key alone) is allowed.
gate_git_config() {
  local k="$1" e="$2" w mode="" v0 v1
  local -a pos posx
  while [ "$k" -lt "$e" ]; do
    w=${words[k]}
    case "$w" in
      --get*|--li*|-l) return 0 ;;
      --un*) mode="unset" ;;
      --rem*) mode="remove" ;;
      --ren*) mode="rename" ;;
      --ed*|-e) mode="edit" ;;
      --file|-f|--blob|--type|--default|--comment|--value|--url) k=$((k+1)) ;;
      -?*) ;;
      *) pos+=("$w"); posx+=("${exps[k]}") ;;
    esac
    k=$((k+1))
  done
  if [ -z "$mode" ] && [ "${#pos[@]}" -gt 0 ]; then
    case "${pos[0]}" in
      get|list|get-color|get-colorbool) return 0 ;;
      set|unset|edit) mode=${pos[0]} ;;
      remove-section) mode=remove ;;
      rename-section) mode=rename ;;
    esac
    [ -n "$mode" ] && { pos=("${pos[@]:1}"); posx=("${posx[@]:1}"); }
  fi
  [ "$mode" = edit ] && deny "git config --edit can change any key, including core.hooksPath, which the commit gate needs. Set the key you need with git config <key> <value>."
  [ "${#pos[@]}" -gt 0 ] || return 0
  [ -n "$mode" ] || [ "${#pos[@]}" -ge 2 ] || return 0       # a key alone reads it
  [ "${posx[0]}" != 0 ] && deny "A git config key holds an expansion this hook cannot check. Spell the key out."
  v0=$(lower "${pos[0]}"); v1=$(lower "${pos[1]:-}")
  case "$mode" in
    unset) [ "$v0" = core.hookspath ] && deny "Commits go through .githooks/pre-commit, which core.hooksPath points at; do not unset it." ;;
    remove) [ "$v0" = core ] && deny "Commits go through .githooks/pre-commit, which core.hooksPath points at; do not remove the core section." ;;
    rename)
      [ "$v0" = core ] && deny "Commits go through .githooks/pre-commit, which core.hooksPath points at; do not rename the core section."
      case "$v1" in alias|alias.*|include*) deny "Do not rename a section to alias or include with git config." ;; esac ;;
    *) case "$v0" in
         core.hookspath|alias|alias.*|include*)
           deny "Commits go through .githooks/pre-commit; do not set core.hooksPath, an alias or a config include with git config." ;;
       esac ;;
  esac
  return 0
}

# uv_gate NORMALISED: deny a `uv run` that carries neither --locked nor --frozen among its own options,
# before the command it runs. uv run --help (uv 0.13.7): --locked asserts uv.lock will remain unchanged;
# --frozen runs without updating it. Without either, uv run re-locks and syncs, so a changed dependency
# rewrites uv.lock.
uv_gate() {
  local seg m k t run locked
  local -a wd
  # shellcheck disable=SC2020  # tr: each of the six characters becomes a newline
  while IFS= read -r seg; do
    case "$seg" in *uv*) ;; *) continue ;; esac
    read -r -a wd <<<"$seg"
    m=${#wd[@]}; k=0
    while [ "$k" -lt "$m" ]; do
      case "${wd[k]}" in
        uv|*/uv)
          t=$((k+1)); run=0; locked=0
          while [ "$t" -lt "$m" ]; do
            case "${wd[t]}" in
              --locked|--frozen|--help|-h) locked=1 ;;            # --help prints and exits
              --) break ;;
              --*=*) ;;
              -*) case "$UV_VALUE_OPTS" in *[[:space:]]"${wd[t]}"[[:space:]]*) t=$((t+1)) ;; esac ;;
              run) [ "$run" = 1 ] && break; run=1 ;;
              *) break ;;
            esac
            t=$((t+1))
          done
          [ "$run" = 1 ] && [ "$locked" = 0 ] \
            && tdeny "uv run without --locked re-locks and can rewrite uv.lock. Add --locked: uv run --locked <command>."
          k=$t ;;
        *) k=$((k+1)) ;;
      esac
    done
  done < <(printf '%s\n' "$1" | tr ';&|()`' '\n\n\n\n\n\n')
}

# pip_gate NORMALISED: deny each command in NORMALISED that installs with pip, unless it asks for --help.
pip_gate() {
  local seg
  # shellcheck disable=SC2020  # tr: each of the six characters becomes a newline
  while IFS= read -r seg; do
    grep -qE "$PIP_INSTALL" <<<"$seg" || continue
    grep -qE '[[:space:]](--help|-h)([[:space:]]|$)' <<<"$seg" && continue
    tdeny "Dependencies come from uv.lock. Use 'uv add <pkg>==<version>' after the adopt-upstream skill, or 'uv sync --locked'."
  done < <(printf '%s\n' "$1" | tr ';&|()`' '\n\n\n\n\n\n')
}

# search_cmd: the split_words callback for simple_search; keeps the words of the first command.
search_cmd() { SN=$((SN+1)); [ "$SN" = 1 ] && { SW=("${W[@]}"); SX=("${X[@]}"); }; return 0; }
# simple_search TEXT: true when TEXT is one grep, rg or git grep command with no chaining, pipe,
# substitution or redirection, and no option that runs another program (rg --pre, git grep -O).
simple_search() {
  local w
  # shellcheck disable=SC2016  # the literal text $( is matched
  case "$1" in *[';&|<>`']*|*'$('*|*$'\n'*) return 1 ;; esac
  SW=(); SX=(); SN=0
  split_words "$1" search_cmd
  [ "$SN" = 1 ] && [ "${SX[0]}" = 0 ] || return 1
  case "${SW[0]}" in
    grep) return 0 ;;
    rg) for w in "${SW[@]}"; do case "$w" in --pre*) return 1 ;; esac; done; return 0 ;;
    git) [ "${SW[1]}" = grep ] && [ "${SX[1]}" = 0 ] || return 1
         for w in "${SW[@]}"; do case "$w" in -O*|--op*|-[!-]*O*) return 1 ;; esac; done; return 0 ;;
  esac
  return 1
}

# tdeny MESSAGE: deny with MESSAGE, or, when the command starts with a search, say that it looks like one.
SEARCH_LIKE=0
tdeny() {
  [ "$SEARCH_LIKE" = 1 ] && deny "That looks like a search that names guarded text. Run the search on its own, without a pipe, chaining, substitution or redirection."
  deny "$1"
}

# cat_to_file TEXT: true when TEXT is exactly `cat > FILE <<'WORD'` (or `cat <<'WORD' > FILE`, spaces
# optional), a body and a closing WORD line, with nothing else, which writes the body to FILE without
# expanding it. Sets CF_PATH and CF_BODY. Only spaces and tabs separate the words; a carriage return makes
# bash read a different delimiter, so text with one on the first line is not this form, and neither is
# <<- or a closing line with a carriage return. Those go through the checks for any other command.
cat_to_file() {
  local first rest d f re1 re2 b=$'[ \t]'
  first=${1%%$'\n'*}; [ "$first" != "$1" ] || return 1
  case "$first" in *$'\r'*) return 1 ;; esac
  rest=${1#*$'\n'}
  re1="^cat$b*>$b*([A-Za-z0-9._/+-]+)$b*<<$b*'([A-Za-z_][A-Za-z0-9_]*)'$b*\$"
  re2="^cat$b*<<$b*'([A-Za-z_][A-Za-z0-9_]*)'$b*>$b*([A-Za-z0-9._/+-]+)$b*\$"
  if [[ $first =~ $re1 ]]; then f=${BASH_REMATCH[1]}; d=${BASH_REMATCH[2]}
  elif [[ $first =~ $re2 ]]; then d=${BASH_REMATCH[1]}; f=${BASH_REMATCH[2]}
  else return 1; fi
  case "$rest" in
    "$d"|"$d"$'\n') CF_BODY="" ;;
    *$'\n'"$d") CF_BODY=${rest%$'\n'"$d"} ;;
    *$'\n'"$d"$'\n') CF_BODY=${rest%$'\n'"$d"$'\n'} ;;
    *) return 1 ;;
  esac
  case $'\n'"$CF_BODY"$'\n' in *$'\n'"$d"$'\n'*) return 1 ;; esac      # the body ends at its first WORD line
  case $'\n'"$CF_BODY" in *$'\n'"$d"$'\r'*) return 1 ;; esac           # a WORD line with a carriage return
  CF_PATH=$f
}

# check_write PATH CONTENT: the rules for new file content, from Edit, Write, NotebookEdit or cat_to_file.
check_write() {
  local nnew p
  p=$(lower "$1" | tr -s '/')                 # .GIT/config and .git//config name the same file
  case "$p" in
    .git/config|*/.git/config|*/.git/config.worktree|.git/config.worktree|*/.git/worktrees/*/config|*/.git/worktrees/*/config.worktree)
      deny "That file is git's own configuration, which holds core.hooksPath for the commit gate. Use git config <key> <value>." ;;
  esac
  nnew=$(norm "$2") || deny "the new content could not be normalised."
  case "$1" in
    *.md) ;;                                  # documentation may name the switches in order to forbid them
    *) grep -qE "$LIVE_ENV" <<<"$nnew" && deny "That change switches Alpaca's CLI or MCP server to live trading. This repo is paper-only (ADR 0009)." ;;
  esac
  case "$1" in
    *.py|*.ipynb)
      if grep -qE "$PY_LIVE" <<<"$nnew" || grep -qE "$LIVE_HOSTS" <<<"$nnew"; then
        deny "That change would point Python code at live trading (paper=False, url_override, a live BaseURL or a live host). This repo is paper-only (ADR 0009)."
      fi
      ;;
  esac
  return 0
}

case "$tool" in
  Bash)
    cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
    re='^[[:space:]]*(grep|egrep|rg|git[[:space:]]+grep)[[:space:]]'
    [[ $cmd =~ $re ]] && SEARCH_LIKE=1
    ncmd=$(norm "$cmd") || deny "the command could not be normalised."
    case "$ncmd" in
      *git*commit*) [ "${#cmd}" -gt "$MAX_COMMIT_CMD" ] \
        && deny "That command is longer than $MAX_COMMIT_CMD characters and holds a git commit. Put the message in a file and use git commit -F <file>." ;;
    esac
    if cat_to_file "$cmd"; then
      check_write "$CF_PATH" "$CF_BODY"
    else
    case "$ncmd" in *git*|*gh*|*scripts/land*) git_gate "$cmd" 0 ;; *) case "$cmd" in *"\$'"*) git_gate "$cmd" 0 ;; esac ;; esac
    if ! simple_search "$cmd"; then
      # the command with message text left out, as the rules below see it
      checked=$cmd
      if [ "${#SA[@]}" -gt 0 ]; then
        a=0
        while [ "$a" -lt "${#SA[@]}" ]; do                   # order the offsets
          b=$((a+1))
          while [ "$b" -lt "${#SA[@]}" ]; do
            if [ "${SA[b]}" -lt "${SA[a]}" ]; then
              t=${SA[a]}; SA[a]=${SA[b]}; SA[b]=$t; t=${SB[a]}; SB[a]=${SB[b]}; SB[b]=$t
            fi
            b=$((b+1))
          done
          a=$((a+1))
        done
        checked=""; at=0; a=0
        while [ "$a" -lt "${#SA[@]}" ]; do
          if [ "${SA[a]}" -ge "$at" ]; then checked="$checked${cmd:at:$((SA[a]-at))} "; fi
          [ "${SB[a]}" -gt "$at" ] && at=${SB[a]}
          a=$((a+1))
        done
        checked="$checked${cmd:at}"
      fi
      ncheck=$(norm "$checked") || deny "the command could not be normalised."
      grep -qE "$PIP_INSTALL" <<<"$ncheck" && pip_gate "$ncheck"
      uv_gate "$ncheck"
      if grep -qE "$LIVE_HOSTS" <<<"$ncheck" || grep -qE "$LIVE_HOSTS" <<<"$(pdecode "$ncheck")"; then
        tdeny "That command names Alpaca's live trading or production broker host. This repo is paper-only (ADR 0009)."
      fi
      if grep -qE "$LIVE_ENV" <<<"$ncheck"; then
        tdeny "That command switches Alpaca's CLI or MCP server to live trading. This repo is paper-only (ADR 0009)."
      fi
      if grep -qE "$PY_LIVE" <<<"$ncheck"; then
        tdeny "That command would run or write Python that leaves paper trading (paper=False, url_override or a live BaseURL). This repo is paper-only (ADR 0009)."
      fi
    fi
    fi
    ;;
  WebFetch)
    rawurl=$(jq -r '.tool_input.url // empty' <<<"$input")
    case "$rawurl" in
      *\\*) deny "That URL holds a backslash, which URL parsers read as a path separator. Write the URL without it." ;;
    esac
    url=$(url_norm "$rawurl") || deny "the URL could not be normalised."
    grep -qE "$LIVE_HOSTS" <<<"$url" && deny "That URL is Alpaca's live trading or production broker host. This repo is paper-only (ADR 0009)."
    ;;
  Edit|Write|NotebookEdit)
    path=$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"$input")
    new=$(jq -r '[.tool_input.content?, .tool_input.new_string?, .tool_input.new_source?] | map(select(. != null)) | join("\n")' <<<"$input")
    check_write "$path" "$new"
    ;;
esac

# ---- Part 2: unattended sessions are read-only -------------------------------------------------------
case "$sid" in ''|*[!A-Za-z0-9_-]*) exit 0 ;; esac          # no usable session id: nothing to look up
[ -e "$root/.canon/unattended/$sid" ] || exit 0

UNATTENDED="This is an unattended run, which is read-only (ADR 0011). Report what you wanted to do instead."
case "$tool" in
  Read|Grep|Glob|WebSearch|Skill|Agent|Task|ToolSearch|TodoWrite|mcp__ccd_turn__*) exit 0 ;;
  WebFetch)
    host=$(printf '%s' "$url" | sed -E 's|^[a-z]+://||; s|[/:?#].*$||')
    for h in $UNATTENDED_HOSTS; do [ "$host" = "$h" ] && exit 0; done
    deny "$UNATTENDED Fetching '$host' is outside the allowed documentation hosts."
    ;;
  Bash)
    cmd=$(jq -r '.tool_input.command // empty' <<<"$input")
    # one simple command, or a pipeline of them: no chaining, substitution, redirection or newlines
    if grep -qE '[;&`<>]|\$\(' <<<"$cmd" || [ "$(printf '%s' "$cmd" | wc -l | tr -d ' ')" != "0" ]; then
      deny "$UNATTENDED Run one plain command at a time: no ';', '&&', redirection or substitution."
    fi
    # Each git subcommand is listed with the only forms that cannot change or run anything. `git branch`
    # takes listing flags only (a bare name would create a branch, -D would delete one). `rg` is left out
    # because `rg --pre` runs a program (ADR 0011).
    ok='^(\./)?scripts/canon (check|lint|list|selftest|outdated( --json)?)$'
    ok="$ok"'|^(\./)?scripts/lanes( --(json|selftest|days [0-9]+|quiet-min [0-9]+))*$'
    ok="$ok"'|^(\./)?scripts/automation status$'
    ok="$ok"'|^git rev-parse( [^|]*)?$'
    ok="$ok"'|^git status( (--short|--porcelain|-s|-sb))*$'
    ok="$ok"'|^git branch( (--show-current|--list|-a|-r|-v|-vv))*$'
    ok="$ok"'|^git (log|show|diff)( [^|]*)?$'
    ok="$ok"'|^(ls|cat|head|tail|grep|jq|wc|date)( [^|]*)?$'
    allowed="scripts/canon check|outdated|lint|list|selftest, scripts/lanes, scripts/automation status, git rev-parse, git status, git branch (listing flags only), git log|show|diff, ls, cat, head, tail, grep, jq, wc, date"
    while IFS= read -r seg; do
      seg=$(sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' <<<"$seg")
      grep -qE -- '--output|--ext-diff|--textconv|--exec|--pre([ =]|-glob|$)' <<<"$seg" \
        && deny "$UNATTENDED That flag writes a file or runs a program."
      grep -qE "$ok" <<<"$seg" || deny "$UNATTENDED Allowed commands: $allowed."
    done < <(tr '|' '\n' <<<"$cmd")
    exit 0
    ;;
  *) deny "$UNATTENDED The tool '$tool' can change things." ;;
esac
