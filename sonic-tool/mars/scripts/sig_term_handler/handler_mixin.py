class TermHandlerMixin:
    def _kill_handler(self, sig, frm):
        """
        Log the killed run's output before TestWrapper's kill handler tears the players down.
        Players only return process output after the process exits, so read the stdout file
        over the player's existing signal rpyc connection (it already uses the topology RPC_PORT,
        which on AIR is a forwarded worker port, not the rpyc default).
        """
        player = self.Players[0]
        self.Logger.info("Termination signal handler started")
        for pid in player.pids:
            try:
                stdout_file = player.process_logger[pid][0]
                self.Logger.info(player.serverProxySignal.modules.files.read(stdout_file))
            except Exception as err:
                self.Logger.error(f"Unable to load test cases run logs from the player: {err}")
        super()._kill_handler(sig, frm)
