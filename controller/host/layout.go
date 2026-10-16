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
)

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
	// StopSeconds is how long HQ is given to stop before it is killed; the
	// web unit's TimeoutStopSec allows more, so systemd never kills the stop.
	StopSeconds = "30"

	// Lib is the root-owned tree a release installs and every unit runs from.
	Lib = "/usr/local/lib/severino-hq"
	// DeployEntry is the one program the runner may run as root.
	DeployEntry = Lib + "/scripts/deploy-image.sh"

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
