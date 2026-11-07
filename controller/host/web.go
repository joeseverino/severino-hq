package host

import (
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
	"path/filepath"
	"time"
)

// Web is the web container as the host runs it. Everything about how it is
// run is here, in one typed place: the unit runs `hq-host run-web`, which
// runs these arguments, and nothing else starts the container.
type Web struct {
	// Image is the digest-pinned reference ReleaseFile names.
	Image string
	// StartPeriod is how long HQ may take to answer healthy; zero is
	// HealthStartPeriod.
	StartPeriod time.Duration
	// SealPublic is the public key the web seals secrets to, root's file
	// (SealPublicKey), when a controller pass has written it. Without it HQ
	// holds no sealed secret.
	SealPublic string
	// TailscaleRun is tailscaled's runtime directory on the host
	// (TailscaleRunDir), on a host that has a tailnet. HQ asks the daemon's
	// socket in it which tailnet device sent each agent call; without it HQ
	// has no tailnet to place a call on, and says so.
	TailscaleRun string
}

// TailscaleRunDir is where tailscaled keeps its local API socket on a Linux
// host, TailscaledSocket, and where the web container is given it: the same
// path, so HQ's setting names the socket on both sides. The directory is
// bound rather than the socket, which the daemon unlinks and makes anew on
// every restart: a bind of the file would keep the old inode.
const (
	TailscaleRunDir  = "/var/run/tailscale"
	TailscaledSocket = TailscaleRunDir + "/tailscaled.sock"
)

// Args are the `docker run` arguments, after "docker". The container is
// removed when it stops and has no restart policy: the unit restarts it, so
// one thing decides when HQ runs.
func (w Web) Args() []string {
	period := w.StartPeriod
	if period == 0 {
		period = HealthStartPeriod
	}
	args := []string{
		"run", "--rm", "--name", hostcore.Container,
		// The real socket peer reaches HQ, not a bridge gateway address.
		"--network", "host",
		"--cap-drop", "ALL",
		"--security-opt", "no-new-privileges:true",
		"--read-only",
		"--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
		// A runaway request loop degrades HQ, not the services beside it.
		"--pids-limit", "512",
		"--memory", "1g",
		// Checked every 2s while starting, not at the image's 30s interval, so
		// the unit is ready when HQ is.
		"--health-start-period", period.String(),
		"--health-start-interval", "2s",
		// Where the database, uploads and exports live is the image's to say
		// (Dockerfile). A request body is spooled whole and an upload written
		// again, so the temporary space is on the data volume, not the
		// RAM-backed /tmp.
		"--env", "TMPDIR=/data/tmp",
		"--env", "SEVERINO_CONTROLLER_DOORBELL=" + hostcore.RunDir + "/controller-doorbell",
		"--env", "SEVERINO_BRIDGE_SOCKET=" + BridgeSocket,
		"--env", "SEVERINO_SCHEMA_SNAPSHOT=/data/" + SchemaSnapshot,
	}
	for _, volume := range Volumes {
		args = append(args, "--mount", "type=volume,source="+volume.Name+",target="+volume.Target)
	}
	appEnv := "/run/secrets/" + hostcore.AppEnvName
	// A bind's source must exist: --mount refuses a missing one instead of
	// creating a directory where a file belongs.
	args = append(args,
		"--mount", "type=bind,source="+hostcore.RunDir+",target="+hostcore.RunDir,
		"--mount", "type=bind,source="+filepath.Join(WebSecretDir, hostcore.AppEnvName)+",target="+appEnv+",readonly",
		// HQ reads its environment from where it is mounted, told here once.
		"--env", "SEVERINO_APP_ENV_PATH="+appEnv,
	)
	if w.SealPublic != "" {
		sealPublic := "/run/secrets/seal_public_key"
		args = append(args,
			"--mount", readOnly(w.SealPublic, sealPublic),
			"--env", "SEVERINO_SEAL_PUBLIC_KEY_FILE="+sealPublic,
		)
	}
	if w.TailscaleRun != "" {
		// Read-only is enough: connecting to a Unix socket needs no write to
		// its mount, and the daemon answers whois to any local caller while
		// only a write to it needs root.
		args = append(args,
			"--mount", readOnly(w.TailscaleRun, TailscaleRunDir),
			"--env", "SEVERINO_TAILSCALED_SOCKET="+TailscaledSocket,
		)
	}
	// No certificate authority: HQ reads none. Provider HTTPS on the private
	// network is the controller's, which builds its bundle from the host's store.
	return append(args, w.Image)
}
