import json
import os
import shlex
import subprocess

from . import solver
from .structures import ctx, JsChallengeResponse, JsChallengeRequest, NChallengeOutput


class DenoJCP:

    def __init__(self):
        self._code_cache = {}
        self._player_cache = {}

    @staticmethod
    def validate_response(response: JsChallengeResponse, request: JsChallengeRequest) -> bool | str:
        if not isinstance(response, JsChallengeResponse):
            return 'Response is not a JsChallengeResponse'
        challenge_output, challenge_input = response.output, request.input
        if not (
            isinstance(challenge_output, NChallengeOutput)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in challenge_output.results.items())
            and challenge_input.challenge in challenge_output.results
        ):
            return 'Invalid NChallengeOutput'

        # Validate n results are valid - if they end with the input challenge then the js function returned with an exception.
        for challenge, result in challenge_output.results.items():
            if result.endswith(challenge):
                return f'n result is invalid for {challenge!r}: {result!r}'
        return True

    def _run_js_runtime(self, player, request) -> str:
        stdin = self._construct_stdin(player, request)
        cmd = ['deno', 'run', '--ext=js', '--no-code-cache', '--no-prompt', '--no-remote',
               '--no-lock', '--node-modules-dir=none', '--no-config', '--no-npm', '--cached-only', '-']
        print(f'Running deno: {shlex.join(cmd)}')
        proc = subprocess.Popen(
            cmd,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ.copy(),
            encoding='utf-8'
        )
        try:
            stdout, stderr = proc.communicate(stdin)
        except BaseException:  # Including KeyboardInterrupt
            proc.kill()
            proc.wait(timeout=0)
            raise

        if proc.returncode or stderr:
            msg = f'Error running deno process (returncode: {proc.returncode})'
            if stderr:
                msg = f'{msg}: {stderr.strip()}'
            raise Exception(msg)
        return stdout

    def _construct_stdin(self, player: str, request: JsChallengeRequest, /) -> str:
        json_requests = [{
            'type': request.type.value,
            'challenges': [request.input.challenge],
        }]
        data = {
            'type': 'player',
            'player': player,
            'requests': json_requests,
            'output_preprocessed': True,
        }

        return f'''\
            {self._get_script('lib')}
            Object.assign(globalThis, lib);
            {self._get_script('core')}
            console.log(JSON.stringify(jsc({json.dumps(data)})));
            '''

    def _get_script(self, script_type: str) -> str:
        try:
            return solver.core() if script_type is 'core' else solver.lib()
        except Exception as e:
            raise ValueError(f'Failed to load challenge solver "{script_type}" script from python package: {e}')

    def _get_player(self, player_url):
        if player_url not in self._code_cache:
            code = ctx.session.http.get(player_url).text
            if code:
                self._code_cache[player_url] = code
        return self._code_cache.get(player_url)

    def solve(self, request: JsChallengeRequest) -> JsChallengeResponse | None:
        """Solves multiple JS Challenges in bulk, returning a list of responses"""
        print(f'Attempting to solve {request.input.challenge} challenges using "Deno" provider')
        try:
            player_url = request.input.player_url
            player = self._get_player(player_url)

            print('Solving JS challenges using Deno')
            stdout = self._run_js_runtime(player, request)
            output = json.loads(stdout)
            if output['type'] == 'error':
                raise Exception(output['error'])

            response_data = output['responses'][0]
            if response_data['type'] == 'error':
                print("Solving JS challenges::if response_data['type'] == 'error':")
                raise Exception(f'ERROR solving JsChallengeRequest({request}); STDOUT={response_data["error"]}')
            else:
                print("Solving JS challenges::if response_data['type'] != 'error':  (OK)")
                response = JsChallengeResponse(request.type, NChallengeOutput(response_data['data']))

            if (vr_msg := self.validate_response(response, request)) is not True:
                print(f'Invalid JS Challenge response received from "Deno" provider: {vr_msg or ""}')
            return response
        except Exception as e:
            print(f"ERROR: {e}")
