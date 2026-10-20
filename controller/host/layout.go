// Package host is what an HQ host is made of: the accounts, paths, names and
// units every host program agrees on, declared once.
package host

import (
	"fmt"
	"regexp"
	"time"
)

// The bounds a boot nests: the web unit waits out one render, then HQ's
// start, and stops HQ within its own stop timeout. The unit files restate
// them, and a test holds each to these.
const (
	// RenderBound is the longest one render runs: the secrets unit's
	// TimeoutStartSec, and how long the web unit waits for its environment.
	RenderBound = 4 * time.Minute
	// HealthStartPeriod is how long HQ may take to answer healthy.
	HealthStartPeriod = 60 * time.Second
	// StartPeriodEnv shortens HealthStartPeriod for one host, so a release
	// that cannot serve fails its start sooner. Only the boot drill sets it.
	StartPeriodEnv = "SEVERINO_HQ_HEALTH_START_PERIOD"
)

// StartPeriod is the health start period StartPeriodEnv asks for:
// HealthStartPeriod when unset, and never longer, since the web unit's start
// timeout is bounded by it.
func StartPeriod(value string) (time.Duration, error) {
	if value == "" {
		return HealthStartPeriod, nil
	}
	period, err := time.ParseDuration(value)
	if err != nil || period <= 0 || period > HealthStartPeriod {
		return 0, fmt.Errorf("%s=%q is not a span of at most %s", StartPeriodEnv, value, HealthStartPeriod)
	}
	return period, nil
}

const (
	// WebUID and WebGID are the account the image runs HQ as. It owns the
	// application environment and the doorbell directory.
	WebUID, WebGID = 10001, 10001

	// Container is the web container's name: the controller launcher and the
	// scheduled jobs find HQ by it.
	Container = "severino-hq"
	// WebUnit runs the web container. It is the only thing that starts it.
	WebUnit = "severino-hq-web.service"
	// SecretsUnit renders the application environment from the vault.
	SecretsUnit = "severino-hq-secrets.service"
	// ControllerUnit runs one controller pass.
	ControllerUnit = "severino-hq-controller.service"
	// BackupUnit snapshots HQ's volumes.
	BackupUnit = "severino-hq-backup.service"
	// DeployFreeBytes is the free space on / a deploy needs before it pulls.
	DeployFreeBytes = 512 << 20
	// StopSeconds is how long HQ is given to stop before it is killed; the
	// web unit's TimeoutStopSec allows more, so systemd never kills the stop.
	StopSeconds = "30"

	// Lib is the root-owned tree a release installs and every unit runs from.
	Lib = "/usr/local/lib/severino-hq"
	// HostProgram is this program, in the tree.
	HostProgram = Lib + "/deploy/bin/hq-host"
	// DeployEntry is what the runner's sudo rules have named; it execs
	// `HostProgram deploy`, which the rule also admits.
	DeployEntry = Lib + "/scripts/deploy-image.sh"
	// DeployArgument is the one argument the rule lets the runner give
	// `hq-host deploy`, as a sudoers regular expression (sudo 1.9.10 or
	// later; an older sudo reads it literally and admits nothing): a
	// digest-pinned composition. Deploy checks the source it names.
	DeployArgument = `^ghcr[.]io/[a-z0-9_.-]+/[a-z0-9_.-]+/composition@sha256\:[0-9a-f]{64}$`

	// SecretDir is the root-only, noswap tmpfs the renderer installs into.
	SecretDir = "/run/severino-hq-secrets"
	// WebSecretDir holds the application environment the web container binds.
	WebSecretDir = SecretDir + "/web"
	// AppEnvName is the application environment's file name, in WebSecretDir
	// on the host and in /run/secrets in the container.
	AppEnvName = "severino_hq_env"
	// RunDir is the doorbell directory HQ and the controller share.
	RunDir = "/run/severino-hq"
	// StateDir holds what the host records about itself across boots.
	StateDir = "/var/lib/severino-hq"
	// ReleaseFile names the image the web unit runs: one digest-pinned
	// reference, written by a deploy, or by an install moving a compose-run
	// host to the unit.
	ReleaseFile = StateDir + "/release"
	// BridgeSocket is where HQ serves the controller's contract, in the
	// bridge volume. The web container is told it, and the controller mounts
	// that volume, from this one declaration.
	BridgeSocket = "/run/hq-bridge/bridge.sock"
	// ACMEDir is the controller's certificate state on the host.
	ACMEDir = "/opt/apps/severino-hq/secrets/acme"
)

// Renderers names each secret renderer on the host and its status document
// in SecretDir. The name is all HQ is told of where a document came from.
var Renderers = []struct{ Name, Status string }{
	{"hq", "status.json"},
}

// UnitProperties is what the controller asks systemd of each unit: states,
// the last run's result and exit, and instants, as `systemctl show` names
// them. None holds a command line, an environment or a path.
var UnitProperties = []string{
	"Id", "LoadState", "UnitFileState", "ActiveState", "SubState", "Result",
	"ExecMainCode", "ExecMainStatus", "InactiveExitTimestamp", "InactiveEnterTimestamp",
	"ConditionResult", "ConditionTimestamp", "LastTriggerUSec", "NextElapseUSecRealtime", "Unit",
}

// Volume is a named docker volume and where the container mounts it. The
// names are the ones the first compose deployment created, so a host keeps
// its data across the change to a unit-run container.
type Volume struct{ Name, Target string }

// Volumes are HQ's state: the database, uploads, exports and the bridge
// socket's directory. A backup reads the same volumes.
var Volumes = []Volume{
	{"severino-hq_severino_data", "/data"},
	{"severino-hq_severino_media", "/media"},
	{"severino-hq_severino_exports", "/exports"},
	{"severino-hq_severino_bridge", "/run/hq-bridge"},
}

// On the data volume: the database, and the copy of it a boot keeps before
// changing its schema, which a release that fails puts back.
const (
	Database       = "severino.sqlite3"
	SchemaSnapshot = Database + ".before-migrate"
)

// pinned is an image reference that names its content: a repository and an
// optional tag, then the digest. A tag alone could be moved under a host.
var pinned = regexp.MustCompile(`^[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[0-9a-f]{64}$`)

// Pinned returns ref when it is a digest-pinned image reference.
func Pinned(ref string) (string, error) {
	if !pinned.MatchString(ref) {
		return "", fmt.Errorf("%q is not a digest-pinned image reference", ref)
	}
	return ref, nil
}
