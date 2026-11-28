package host

import (
	"crypto/rand"
	"os"
	"path/filepath"
	"regexp"
)

// HQ's own containers. Each run of hq-host that starts containers (a deploy,
// a pass, a worker, a dependency run) registers a nonce in RunsDir, as an
// empty file of that name, for as long as it runs, and labels each of its
// containers RunLabel=nonce. A pass leaves out of what it finds any container
// whose label names a registered run, or a run that ended since the pass
// listed it and whose container Docker no longer has, so HQ never reports its
// own work as a container nobody started.
//
// Its security properties:
//   - Only root registers a run, and each nonce is drawn fresh (rand.Text).
//     RunsDir can be searched but not listed (0711), and a label is held to
//     RunNonce before it is looked up, so it names a file directly in RunsDir
//     or nothing.
//   - So a container someone else makes names no registered run, whatever
//     value it gives the label, and is swept as any other: one no compose
//     project declares is an unrecognized container.
//   - A nonce is readable on its run's containers, so a container that copies
//     one is left out while that run is registered: until it ends, or until
//     boot for a run killed before it removed its file. Making a container
//     takes Docker access, which is root's already.
const (
	RunLabel = "severino-hq.run"
	// RunsDir is the registry on a host. The tmpfiles rule makes it at boot,
	// as beginRun does on a host whose rule predates it.
	RunsDir = "/run/severino-hq-runs"
)

// RunNonce is a nonce as rand.Text draws it.
var RunNonce = regexp.MustCompile(`^[A-Z2-7]{26,}$`)

// ownRun is one run registered in the Machine's Runs.
type ownRun struct{ nonce, file string }

// beginRun registers a run; end removes it once its containers are gone. A
// run the registry cannot take stays unregistered: its containers are then
// reported as anyone's are, and the run itself goes on.
func (m *Machine) beginRun() ownRun {
	run := ownRun{nonce: rand.Text()}
	if os.MkdirAll(m.Runs, 0o711) == nil {
		if file, err := os.OpenFile(filepath.Join(m.Runs, run.nonce), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600); err == nil {
			run.file = file.Name()
			_ = file.Close()
		}
	}
	return run
}

// label is the docker run or create option that marks a container as this run's.
func (r ownRun) label() string { return "--label=" + RunLabel + "=" + r.nonce }

func (r ownRun) end() {
	if r.file != "" {
		_ = os.Remove(r.file)
	}
}
