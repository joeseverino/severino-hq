// Command hq-edge is the receiver on a Caddy edge HQ's controller deploys
// certificates and routes to. An administrator installs it root-owned as
// /usr/local/sbin/severino-hq-edge-controller, where the edge key's forced
// command (deploy/targets/severino-hq-edge-dispatch) runs it through sudo
// with the client's command as its one argument. What it trusts is the edge
// package's to state.
package main

import (
	"context"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"os"
	"os/signal"
	"syscall"

	"github.com/joeseverino/severino-hq/controller/edge"
)

func main() {
	// A program is found on root's own PATH, never the caller's.
	if err := os.Setenv("PATH", hostcore.RootPath); err != nil {
		os.Exit(1)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	code := edge.Defaults(os.Getenv).Run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}
