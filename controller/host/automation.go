package host

import (
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/internal/atomicfile"
	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
)

// Where the worker's container finds what it is given, each the mount's
// target and what the variable hq_runner reads names (hq_runner/__main__.py).
const (
	automationConnections = "/run/secrets/automation-connections.json"
	automationSSH         = "/run/secrets/automation-ssh"
	automationToken       = "/run/secrets/automation-token" //nolint:gosec // G101: a directory's path
	automationTokenName   = "github_token"
	automationJournal     = "/var/lib/hq-automation"
	automationScratch     = "/tmp"
)

// AutomationConfig is where the worker's procedures come from, in the host's
// configuration: a GitHub repository, the directory in it the playbooks run
// from, and the host grant whose token fetches it, none for a public one.
// Only root's configuration names them; a run's claim never does.
type AutomationConfig struct {
	Repository string `json:"repository"`
	Directory  string `json:"directory,omitempty"`
	Grant      string `json:"grant,omitempty"`
}

// projectPart is one name of the project directory, as the worker holds it
// (hq_runner/procedures.py:safe_path).
var projectPart = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]*$`)

// checkAutomation refuses an automation entry unless its repository is one
// GitHub could name, its directory is plain names inside it, and its grant is
// one of the host's that reads that repository.
func checkAutomation(automation *AutomationConfig, grants []Grant) error {
	if automation == nil {
		return nil
	}
	if err := checkRepositories([]string{automation.Repository}); err != nil {
		return fmt.Errorf("automation.repository: %w", err)
	}
	if automation.Directory != "" {
		for part := range strings.SplitSeq(automation.Directory, "/") {
			if !projectPart.MatchString(part) {
				return errors.New("automation.directory must be a relative path of plain names")
			}
		}
	}
	if automation.Grant == "" {
		return nil
	}
	for _, grant := range grants {
		if grant.Name != automation.Grant {
			continue
		}
		reads := slices.ContainsFunc(grant.Repositories, func(repository string) bool {
			return strings.EqualFold(repository, automation.Repository)
		})
		if !reads || grant.Permissions["contents"] == "" {
			return fmt.Errorf("automation.grant %s does not read %s: it needs the repository and contents", grant.Name, automation.Repository)
		}
		return nil
	}
	return fmt.Errorf("automation.grant %s is none of the host's grants", automation.Grant)
}

// Automation runs the automation worker: hq_runner, in a container of its own
// image, until no run is queued. Its container is given the runner-role
// connections and their identities, the token that fetches its procedures,
// its journal and the bridge; it reaches those connections and public
// addresses alone.
type Automation struct {
	*Machine
	// SecretDir is the renderer's tmpfs (SecretDir on a host).
	SecretDir string
	// Journal is the worker's journal directory (AutomationState).
	Journal string
	// Project is the host configuration's automation entry, nil when the
	// host runs no worker, and Grants the host's own grants it names one of.
	Project *AutomationConfig
	Grants  []Grant
	// ID is the name the worker claims runs as: the host's.
	ID string
	// Bound is AutomationBound on a host, and TokenPoll how often a running
	// worker's token is brought up to date with root's.
	Bound, TokenPoll time.Duration
	// Connections checks the connections document before it is read, and
	// Lock holds the renderer's identity lock, as a controller pass's do.
	Connections func() error
	Lock        func(ctx context.Context) (func(), error)
	// Stdout and Stderr receive the worker's own output.
	Stdout, Stderr io.Writer
}

// Run launches the worker once, if this host runs one.
func (a *Automation) Run(ctx context.Context) error {
	if a.Project == nil {
		fmt.Fprintln(a.Stdout, "This host runs no automation worker: its configuration has no automation entry.")
		return nil
	}
	pin, err := os.ReadFile(filepath.Join(a.Lib, AutomationImage))
	if errors.Is(err, fs.ErrNotExist) {
		fmt.Fprintln(a.Stdout, "This release ships no automation worker image.")
		return nil
	}
	if err != nil {
		return err
	}
	image, err := Pinned(strings.TrimSpace(string(pin)))
	if err != nil {
		return err
	}
	if info, err := os.Lstat(a.Journal); err != nil || !info.IsDir() {
		return fmt.Errorf("the worker's journal directory %s is not there: systemd-tmpfiles --create makes it", a.Journal)
	}
	a.pruneRuns()
	run, err := os.MkdirTemp(a.SecretDir, "automation.")
	if err != nil {
		return err
	}
	defer os.RemoveAll(run)

	lent, err := lendConnections(ctx, a.Lock, a.Connections, a.SecretDir, run, a.reaches)
	if err != nil {
		return err
	}
	token, err := a.grantToken()
	if err != nil {
		return err
	}
	if token != "" {
		if err := os.Mkdir(filepath.Join(run, "token"), 0o700); err != nil {
			return err
		}
		if _, err := a.lendToken(run, token, nil); err != nil {
			return err
		}
	}
	if err := a.ownTree(run); err != nil {
		return err
	}
	confined := automationEgress
	confined.reach = reach(lent)
	if err := confined.confine(ctx, a.Machine); err != nil {
		return err
	}
	name := "severino-hq-automation-" + rand.Text()
	return a.work(ctx, name, AutomationArgs(name, image, run, a.Journal, a.ID, *a.Project), run, token)
}

// work runs the container until it ends or the bound passes, keeping its
// token current meanwhile; a container its client stopped waiting on is
// removed.
func (a *Automation) work(ctx context.Context, name string, args []string, run, token string) error {
	runCtx, cancel := context.WithTimeout(ctx, a.Bound)
	defer cancel()
	ended := make(chan struct{})
	if token != "" {
		go a.keepToken(runCtx, ended, run, token)
	}
	command := a.Command(runCtx, "docker", args...)
	command.Stdout, command.Stderr = a.Stdout, a.Stderr
	err := command.Run()
	close(ended)
	if runCtx.Err() != nil {
		stop, done := context.WithTimeout(context.WithoutCancel(ctx), time.Minute)
		_ = a.Command(stop, "docker", "rm", "--force", name).Run()
		done()
		return fmt.Errorf("the automation worker was stopped: %w", runCtx.Err())
	}
	return err
}

// reaches is whether the worker is lent a connection: a runner-role SSH
// transport whose host the egress table can name, an IPv4 address. One
// named otherwise is said and left out, so a run that needs it is refused
// as out of reach rather than dropped on the wire.
func (a *Automation) reaches(connection connections.Connection) bool {
	if !runnerTransport(connection) {
		return false
	}
	if _, ok := destination(connection); !ok {
		fmt.Fprintf(a.Stderr, "The automation worker is not lent %s: its host must be an IPv4 address and its port a number, which the egress table names.\n", connection)
		return false
	}
	return true
}

// destination is where a runner connection's SSH transport dials.
func destination(connection connections.Connection) (netip.AddrPort, bool) {
	address, err := netip.ParseAddr(connection.SSHTransport.Host)
	port, portErr := strconv.ParseUint(connection.SSHTransport.Port, 10, 16)
	if err != nil || !address.Is4() || portErr != nil || port == 0 {
		return netip.AddrPort{}, false
	}
	return netip.AddrPortFrom(address, uint16(port)), true
}

// reach is the destinations of the lent connections, sorted and once each.
func reach(lent []connections.Connection) []netip.AddrPort {
	var reach []netip.AddrPort
	for _, connection := range lent {
		if to, ok := destination(connection); ok && !slices.Contains(reach, to) {
			reach = append(reach, to)
		}
	}
	slices.SortFunc(reach, func(a, b netip.AddrPort) int { return a.Compare(b) })
	return reach
}

// grantToken is the path of the token that fetches the worker's procedures,
// blank for a public repository. A grant whose token root has not placed yet
// is an error: the applying pass that hears of it mints one.
func (a *Automation) grantToken() (string, error) {
	if a.Project.Grant == "" {
		return "", nil
	}
	for _, grant := range a.Grants {
		if grant.Name != a.Project.Grant {
			continue
		}
		if info, err := os.Lstat(grant.Path); err != nil || !info.Mode().IsRegular() {
			return "", fmt.Errorf("the grant %s has no token on this host yet; an applying controller pass mints it", grant.Name)
		}
		return grant.Path, nil
	}
	return "", fmt.Errorf("the grant %s is none of the host's", a.Project.Grant)
}

// lendToken puts the token at path in the run's token directory, the
// worker's to read, when it is not what was lent before, and answers what is
// lent now. The copy is the worker's before it takes the name, so a read
// never finds a file it may not open.
func (a *Automation) lendToken(run, path string, before []byte) ([]byte, error) {
	data, err := os.ReadFile(path)
	if err != nil || bytes.Equal(data, before) {
		return before, err
	}
	dir := filepath.Join(run, "token")
	file, err := atomicfile.Create(filepath.Join(dir, automationTokenName), 0o400)
	if err != nil {
		return before, err
	}
	defer file.Discard()
	_, err = file.Write(data)
	if err == nil {
		err = a.ownName(dir, filepath.Base(file.Temporary()))
	}
	if err == nil {
		err = file.Replace()
	}
	if err != nil {
		return before, err
	}
	return data, nil
}

func (a *Automation) ownName(dir, name string) error {
	root, err := os.OpenRoot(dir)
	if err != nil {
		return err
	}
	defer root.Close()
	return a.Owner(root, name)
}

// keepToken brings the lent token up to date with root's until the worker
// ends: a token lives an hour and a worker may run longer, and the worker
// reads the file again before each fetch.
func (a *Automation) keepToken(ctx context.Context, ended <-chan struct{}, run, path string) {
	lent, _ := os.ReadFile(path)
	tick := time.NewTicker(a.TokenPoll)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ended:
			return
		case <-tick.C:
			if now, err := a.lendToken(run, path, lent); err == nil {
				lent = now
			} else {
				fmt.Fprintf(a.Stderr, "The automation worker's token was not brought up to date: %v\n", err)
			}
		}
	}
}

// pruneRuns removes what a worker killed before its cleanup left behind: the
// unit runs one at a time, so any run directory left is no live one's.
func (a *Automation) pruneRuns() {
	left, _ := filepath.Glob(filepath.Join(a.SecretDir, "automation.*"))
	for _, dir := range left {
		os.RemoveAll(dir)
	}
}

// AutomationArgs is the worker's container: the web account, which the
// bridge's peer check requires, with no capability and no way to gain one;
// a read-only root with only a scratch tmpfs and its journal to write; its
// own network, from which the egress table lets it reach its runner
// connections and public addresses alone; public resolvers; and of the
// host's, the lent connections, identities and token, read-only, and the
// bridge.
func AutomationArgs(name, image, run, journal, id string, project AutomationConfig) []string {
	args := []string{
		"run", "--rm", "--name", name,
		"--user", fmt.Sprintf("%d:%d", hostcore.WebUID, hostcore.WebGID),
		"--cap-drop", "ALL",
		"--security-opt", "no-new-privileges:true",
		"--read-only",
		"--no-healthcheck",
		"--label", "severino-hq.role=automation",
		"--network", AutomationNetwork,
	}
	for _, resolver := range PublicResolvers {
		args = append(args, "--dns", resolver)
	}
	args = append(args,
		"--memory", "512m", "--pids-limit", "512",
		"--tmpfs", automationScratch+":size=256m,noexec,nosuid,nodev",
		"--mount", readOnly(filepath.Join(run, "connections.json"), automationConnections),
		"--mount", readOnly(filepath.Join(run, "ssh"), automationSSH),
		"--mount", "type=bind,source="+journal+",target="+automationJournal,
		"--mount", "type=volume,source="+bridgeVolume()+",target="+filepath.Dir(BridgeSocket)+",readonly",
		"--env", "HQ_AUTOMATION_ID="+id,
		"--env", "HQ_AUTOMATION_BRIDGE_SOCKET="+BridgeSocket,
		"--env", "HQ_AUTOMATION_JOURNAL="+automationJournal+"/journal.json",
		"--env", "HQ_AUTOMATION_CONNECTIONS="+automationConnections,
		"--env", "HQ_AUTOMATION_SSH_DIR="+automationSSH,
		"--env", "HQ_AUTOMATION_SCRATCH="+automationScratch,
		"--env", "HQ_AUTOMATION_REPOSITORY="+project.Repository,
	)
	if project.Directory != "" {
		args = append(args, "--env", "HQ_AUTOMATION_PROJECT_DIR="+project.Directory)
	}
	if project.Grant != "" {
		args = append(args,
			"--mount", readOnly(filepath.Join(run, "token"), automationToken),
			"--env", "HQ_AUTOMATION_TOKEN_FILE="+automationToken+"/"+automationTokenName)
	}
	return append(args, "--entrypoint", "python", image, "-m", "hq_runner")
}
