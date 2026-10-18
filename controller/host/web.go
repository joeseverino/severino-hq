package host

import (
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
}

// Args are the `docker run` arguments, after "docker". The container is
// removed when it stops and has no restart policy: the unit restarts it, so
// one thing decides when HQ runs.
func (w Web) Args() []string {
	period := w.StartPeriod
	if period == 0 {
		period = HealthStartPeriod
	}
	args := []string{
		"run", "--rm", "--name", Container,
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
		"--env", "SEVERINO_CONTROLLER_DOORBELL=" + RunDir + "/controller-doorbell",
		"--env", "SEVERINO_BRIDGE_SOCKET=" + BridgeSocket,
		"--env", "SEVERINO_SCHEMA_SNAPSHOT=/data/" + SchemaSnapshot,
	}
	for _, volume := range Volumes {
		args = append(args, "--mount", "type=volume,source="+volume.Name+",target="+volume.Target)
	}
	appEnv := "/run/secrets/" + AppEnvName
	// A bind's source must exist: --mount refuses a missing one instead of
	// creating a directory where a file belongs.
	args = append(args,
		"--mount", "type=bind,source="+RunDir+",target="+RunDir,
		"--mount", "type=bind,source="+filepath.Join(WebSecretDir, AppEnvName)+",target="+appEnv+",readonly",
		// HQ reads its environment from where it is mounted, told here once.
		"--env", "SEVERINO_APP_ENV_PATH="+appEnv,
	)
	// No certificate authority: HQ reads none. Provider HTTPS on the private
	// network is the controller's, which builds its bundle from the host's store.
	return append(args, w.Image)
}
