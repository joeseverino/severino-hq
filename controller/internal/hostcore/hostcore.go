// Package hostcore is the part of an HQ host that root's smallest programs
// share with hq-host: the web account, the names and paths they agree on, and
// the one way a controller binary starts a program. It imports only the
// standard library, so hq-secrets and hq-edge link nothing of host's own
// dependencies.
package hostcore

const (
	// WebUID and WebGID are the account the image runs HQ as. It owns the
	// application environment and the doorbell directory.
	WebUID = 10001
	WebGID = 10001

	// Container is the web container's name: the controller launcher and the
	// scheduled jobs find HQ by it.
	Container = "severino-hq"
	// WebUnit runs the web container. It is the only thing that starts it.
	WebUnit = "severino-hq-web.service"

	// SecretDir is the root-only, noswap tmpfs the renderer installs into.
	SecretDir = "/run/severino-hq-secrets" //nolint:gosec // G101: a directory's path
	// AppEnvName is the application environment's file name, in the web's
	// secret directory on the host and in /run/secrets in the container.
	AppEnvName = "severino_hq_env"
	// RunDir is the doorbell directory HQ and the controller share.
	RunDir = "/run/severino-hq"
)
