%
% This file is part of AtomVM.
%
% Copyright 2026 Paul Guyot <pguyot@kallisys.net>
%
% Licensed under the Apache License, Version 2.0 (the "License");
% you may not use this file except in compliance with the License.
% You may obtain a copy of the License at
%
%    http://www.apache.org/licenses/LICENSE-2.0
%
% Unless required by applicable law or agreed to in writing, software
% distributed under the License is distributed on an "AS IS" BASIS,
% WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
% See the License for the specific language governing permissions and
% limitations under the License.
%
% SPDX-License-Identifier: Apache-2.0 OR LGPL-2.1-or-later
%

%% @doc An erlc-compatible compiler front-end that runs on AtomVM, using the
%% bundled Erlang/OTP compiler. Supports the common erlc options:
%%   erlc [-o outdir] [-I dir] [-D name[=value]] [-W*] [+term] file.erl...
-module(atomvm_erlc).

-export([main/1, start/0]).

main(Args) ->
    case parse_args(Args, #{out => ".", includes => [], defines => [], opts => [], files => []}) of
        {ok, #{files := []}} ->
            usage(),
            error;
        {ok, Config} ->
            compile_files(Config);
        {error, Reason} ->
            io:format("atomvm_erlc: ~s~n", [Reason]),
            usage(),
            error
    end.

%% Entry point for the emscripten (Node.js) build. There, AtomVM's
%% platform main does not forward the process argv to the Erlang program --
%% it runs this start/0 with no arguments -- so the Node wrapper passes the
%% command line through the ATOMVM_ERLC_ARGV environment variable instead,
%% one argument per line. The native build never arrives here with that
%% variable meaningful: it enters through main/1 with the real argv (the
%% escript convention), so reading the variable only affects the Node build.
start() ->
    main(env_args("ATOMVM_ERLC_ARGV")).

env_args(Var) ->
    case os:getenv(Var) of
        Value when is_list(Value), Value =/= "" ->
            split_lines(Value, [], []);
        _ ->
            []
    end.

split_lines([$\n | Rest], Cur, Acc) ->
    split_lines(Rest, [], [lists:reverse(Cur) | Acc]);
split_lines([C | Rest], Cur, Acc) ->
    split_lines(Rest, [C | Cur], Acc);
split_lines([], Cur, Acc) ->
    lists:reverse([lists:reverse(Cur) | Acc]).

usage() ->
    io:format(
        "Usage: erlc [options] file.erl...~n"
        "Options:~n"
        "  -o dir        output directory~n"
        "  -I dir        add dir to include path~n"
        "  -Dname        define macro~n"
        "  -Dname=value  define macro with value~n"
        "  -v            verbose~n"
        "  -W0           disable warnings~n"
        "  +term         add term to compile options~n"
    ).

parse_args([], Config) ->
    {ok, Config};
parse_args(["-o", Dir | Rest], Config) ->
    parse_args(Rest, Config#{out := Dir});
parse_args(["-o" ++ Dir | Rest], Config) when Dir =/= "" ->
    parse_args(Rest, Config#{out := Dir});
parse_args(["-I", Dir | Rest], #{includes := Inc} = Config) ->
    parse_args(Rest, Config#{includes := Inc ++ [Dir]});
parse_args(["-I" ++ Dir | Rest], #{includes := Inc} = Config) when Dir =/= "" ->
    parse_args(Rest, Config#{includes := Inc ++ [Dir]});
parse_args(["-D" ++ Def | Rest], #{defines := Defs} = Config) when Def =/= "" ->
    Define =
        case string:split(Def, "=") of
            [Name] -> {d, list_to_atom(Name)};
            [Name, Value] -> {d, list_to_atom(Name), parse_term_or_string(Value)}
        end,
    parse_args(Rest, Config#{defines := Defs ++ [Define]});
parse_args(["-W0" | Rest], #{opts := Opts} = Config) ->
    parse_args(Rest, Config#{opts := [no_warnings | Opts]});
parse_args(["-v" | Rest], #{opts := Opts} = Config) ->
    parse_args(Rest, Config#{opts := [verbose | Opts]});
parse_args(["-W" ++ _ | Rest], Config) ->
    % warning level adjustments: default behavior
    parse_args(Rest, Config);
parse_args(["+" ++ TermStr | Rest], #{opts := Opts} = Config) ->
    case parse_term(TermStr) of
        {ok, Term} -> parse_args(Rest, Config#{opts := Opts ++ [Term]});
        error -> {error, io_lib:format("bad +option: ~s", [TermStr])}
    end;
parse_args(["-" ++ _ = Opt | _Rest], _Config) ->
    {error, io_lib:format("unknown option: ~s", [Opt])};
parse_args([File | Rest], #{files := Files} = Config) ->
    parse_args(Rest, Config#{files := Files ++ [File]}).

parse_term_or_string(Value) ->
    case parse_term(Value) of
        {ok, Term} -> Term;
        error -> Value
    end.

parse_term(Str) ->
    case erl_scan:string(Str ++ ".") of
        {ok, Tokens, _} ->
            case erl_parse:parse_term(Tokens) of
                {ok, Term} -> {ok, Term};
                _ -> error
            end;
        _ ->
            error
    end.

compile_files(#{files := Files} = Config) ->
    Results = [compile_one_in_proc(File, Config) || File <- Files],
    case lists:all(fun(R) -> R =:= ok end, Results) of
        true -> ok;
        false -> error
    end.

%% Compile in a dedicated process so each file starts from a fresh heap. The
%% worker exits with reason `normal` (passing its result by message) so the VM
%% does not format/print a crash report per file — a non-trivial fixed cost when
%% compiling many files, and noise that BEAM's erlc never emits.
compile_one_in_proc(File, Config) ->
    Parent = self(),
    {Pid, Ref} = spawn_opt(
        fun() -> Parent ! {self(), compile_one(File, Config)} end,
        [monitor, {min_heap_size, 2000000}]
    ),
    receive
        {Pid, Result} ->
            receive
                {'DOWN', Ref, process, Pid, _} -> ok
            end,
            Result;
        {'DOWN', Ref, process, Pid, Reason} ->
            % worker died before sending a result (e.g. out of memory)
            {error, Reason}
    end.

compile_one(File, #{out := Out, includes := Includes, defines := Defines, opts := ExtraOpts}) ->
    WarnOpts =
        case lists:member(no_warnings, ExtraOpts) of
            true -> [];
            false -> [report_warnings]
        end,
    Opts =
        % no_spawn_compiler_process: this front-end already runs each file in
        % its own process (compile_one_in_proc), so the compiler's internal
        % worker spawn is redundant -- and its exit({ok,Module}) would trigger a
        % per-file crash report. Running in-process avoids both.
        [report_errors, no_spawn_compiler_process, {outdir, Out}]
            ++ WarnOpts
            ++ [{i, Dir} || Dir <- Includes]
            ++ Defines
            ++ [Opt || Opt <- ExtraOpts, Opt =/= no_warnings],
    case compile:file(File, Opts) of
        {ok, _Module} ->
            ok;
        {ok, _Module, _Warnings} ->
            ok;
        error ->
            error;
        {error, _Errors, _Warnings} ->
            error
    end.
