% =====================================================================
% NLCL semantic knowledge base - core rules (standard Prolog).
% Loaded by nlcl/knowledge.py into the embedded engine; also loadable in
% SWI-Prolog after `:kb export` (which adds the facts below as clauses).
%
% Facts asserted at start-up from the Python side:
%   atomic(Word, Token).              core lexicon (tokens.py)
%   kind_of(Kind, Domain, Strength).  ontology from the modules: 2 = kind, 1 = keyword
%   alias(Word, Domain).              words that name a domain
% Learned facts (learned.pl, written by the trainer / by confirmations):
%   rewrite(FromWords, ToWords, Support).   learned meaning: [get,rid,of] -> [delete]
%   reliability(Key, Successes, Failures).  evidence for a token / frame / intent
%   param(Name, Value).                     parameters of the sentence-weighting model
%   learned_module(Name, Language, Ext).    modules discovered at run time
% =====================================================================

:- dynamic rewrite/3.
:- dynamic reliability/3.
:- dynamic param/2.
:- dynamic learned_module/3.

% ---------------------------------------------------------------- domains
domain_candidates(Kind, Ds) :- findall(D, kind_of(Kind, D, 2), L), L \= [], !, sort(L, Ds).
domain_candidates(Kind, Ds) :- findall(D, kind_of(Kind, D, 1), L), sort(L, Ds).
unambiguous_domain(Kind, D) :- domain_candidates(Kind, [D]).
ambiguous_kind(Kind) :- domain_candidates(Kind, [_, _ | _]).

% ---------------------------------------------------------------- reliability
% Beta(1,1) posterior mean of the evidence collected for a key.
reliable(Key, P) :- reliability(Key, S, F), !, P is (S + 1) / (S + F + 2).
reliable(Key, P) :- prior(Key, P), !.
reliable(_, 0.75).

prior(token(_), 0.9).
prior(frame(_), 0.85).
prior(rewrite(_), 0.7).
prior(intent(_), 0.85).

% a learned rewrite may not turn a core word into a different core meaning
conflict(From, To) :- rewrite([From], [To], _), atomic(From, T1), atomic(To, T2), T1 \== T2.

% ---------------------------------------------------------------- terminal: meaning of intents
% shell_intent(Intent, SafetyClass, Description).
shell_intent(list_files,   read,    'list files or folders').
shell_intent(count_files,  read,    'count files').
shell_intent(show_file,    read,    'print a file (or its first / last lines)').
shell_intent(count_lines,  read,    'count the lines of a file').
shell_intent(search_text,  read,    'find text inside files').
shell_intent(find_large,   read,    'find files larger than a size').
shell_intent(disk_usage,   read,    'size of a folder').
shell_intent(sort_lines,   read,    'print the lines of a file sorted').
shell_intent(current_dir,  read,    'print the working folder').
shell_intent(make_dir,     create,  'create a folder').
shell_intent(make_file,    create,  'create a file, optionally with text').
shell_intent(append_text,  modify,  'add a line to the end of a file').
shell_intent(copy,         create,  'copy files or folders').
shell_intent(archive,      create,  'pack files into a zip archive').
shell_intent(move,         modify,  'move or rename files').
shell_intent(replace_text, modify,  'replace text inside a file').
shell_intent(delete,       destroy, 'delete files or folders').

changes_files(I) :- shell_intent(I, S, _), S \== read.
needs_confirmation(I) :- shell_intent(I, S, _), memberchk(S, [modify, destroy]).

% paths a destructive command may never touch
protected_path('/'). protected_path('~'). protected_path('.'). protected_path('..').
protected_path('*'). protected_path('.git'). protected_path('C:/'). protected_path('C:\\').
forbidden(I, P) :- shell_intent(I, destroy, _), protected_path(P).
forbidden(I, P) :- shell_intent(I, destroy, _), sub_atom(P, 0, _, _, '.git/').

% ---------------------------------------------------------------- terminal: words for file types
file_type(python,     ['*.py']).
file_type(py,         ['*.py']).
file_type(javascript, ['*.js']).
file_type(js,         ['*.js']).
file_type(typescript, ['*.ts']).
file_type(text,       ['*.txt']).
file_type(txt,        ['*.txt']).
file_type(markdown,   ['*.md']).
file_type(md,         ['*.md']).
file_type(json,       ['*.json']).
file_type(csv,        ['*.csv']).
file_type(log,        ['*.log']).
file_type(temporary,  ['*.tmp']).
file_type(temp,       ['*.tmp']).
file_type(tmp,        ['*.tmp']).
file_type(backup,     ['*.bak']).
file_type(bak,        ['*.bak']).
file_type(html,       ['*.html']).
file_type(css,        ['*.css']).
file_type(c,          ['*.c']).
file_type(cpp,        ['*.cpp']).
file_type(header,     ['*.h']).
file_type(java,       ['*.java']).
file_type(rust,       ['*.rs']).
file_type(go,         ['*.go']).
file_type(shell,      ['*.sh']).
file_type(yaml,       ['*.yml']).
file_type(xml,        ['*.xml']).
file_type(pdf,        ['*.pdf']).
file_type(zip,        ['*.zip']).
file_type(image,      ['*.png', '*.jpg', '*.jpeg', '*.gif']).
file_type(picture,    ['*.png', '*.jpg', '*.jpeg', '*.gif']).
file_type(photo,      ['*.png', '*.jpg', '*.jpeg', '*.gif']).
file_type(verilog,    ['*.v']).
file_type(arduino,    ['*.ino']).

% ---------------------------------------------------------------- module discovery
% language(Name, Extension, RunHint) - targets the system can grow a module for
language(rust,       '.rs',    'cargo run / rustc main.rs').
language(go,         '.go',    'go run .').
language(java,       '.java',  'javac *.java && java Main').
language(kotlin,     '.kt',    'kotlinc *.kt -include-runtime -d app.jar').
language(csharp,     '.cs',    'dotnet run').
language(typescript, '.ts',    'npx tsc && node main.js').
language(javascript, '.js',    'node main.js').
language(ruby,       '.rb',    'ruby main.rb').
language(php,        '.php',   'php main.php').
language(lua,        '.lua',   'lua main.lua').
language(swift,      '.swift', 'swift main.swift').
language(dart,       '.dart',  'dart run').
language(julia,      '.jl',    'julia main.jl').
language(r,          '.R',     'Rscript main.R').
language(haskell,    '.hs',    'runghc Main.hs').
language(scala,      '.scala', 'scala main.scala').
language(elixir,     '.ex',    'elixir main.ex').
language(zig,        '.zig',   'zig run main.zig').
language(prolog,     '.pl',    'swipl main.pl').
language(sql,        '.sql',   'sqlite3 app.db < schema.sql').
language(bash,       '.sh',    'bash main.sh').
language(powershell, '.ps1',   'powershell -File main.ps1').
language(matlab,     '.m',     'octave main.m').

discoverable(Name) :- language(Name, _, _), \+ learned_module(Name, _, _).
