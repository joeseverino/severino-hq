package host

import (
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"maps"
	"net/netip"
	"os"
	"path/filepath"
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
// configuration: a GitHub repository, whose root is an Ansible project, and
// the host grant whose token fetches it, none for a public one.
// Only root's configuration names them; a run's claim never does.
type AutomationConfig struct {
	Repository string `json:"repository"`
	Grant      string `json:"grant,omitempty"`
}

// checkAutomation refuses an automation entry unless its repository is one
// GitHub could name and its grant is one of the host's that reads that
// repository and may do nothing but read.
func checkAutomation(automation *AutomationConfig, grants []Grant) error {
	if automation == nil {
		return nil
	}
	if err := checkRepositories([]string{automation.Repository}); err != nil {
		return fmt.Errorf("automation.repository: %w", err)
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
		// The worker only fetches, and a playbook's tasks can read its token,
		// so a token that could write is refused.
		for _, permission := range slices.Sorted(maps.Keys(grant.Permissions)) {
			if level := grant.Permissions[permission]; level != "read" {
				return fmt.Errorf("automation.grant %s may %s %s: the worker's grant reads and nothing more", grant.Name, level, permission)
			}
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
	// Source is the repository whose runner image a release may pin.
	Source Source
	// SecretDir is the renderer's tmpfs (SecretDir on a host).
	SecretDir string
	// Journal is the worker's journal directory (AutomationState), and
	// Mounts reads the mount table (hostcore.MountTable), which must hold it
	// as a noexec mount of its own.
	Journal string
	Mounts  func() ([]byte, error)
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
	if err != nil {
		return err
	}
	image, err := a.Source.RunnerImage(strings.TrimSpace(string(pin)))
	if err != nil {
		return err
	}
	// The deploy that installed this tree verified the image's signature and
	// pulled it, and the container runs it with --pull never, so nothing a
	// registry answers now can take its place.
	if a.Command(ctx, "docker", "image", "inspect", "--format", "{{.Id}}", image).Run() != nil {
		return fmt.Errorf("the automation worker's image %s is not on this host: `hq-host deploy` verifies and pulls it", image)
	}
	if info, err := os.Lstat(a.Journal); err != nil || !info.IsDir() {
		return fmt.Errorf("the worker's journal directory %s is not there: systemd-tmpfiles --create makes it", a.Journal)
	}
	// The one host path the worker writes, so nothing it writes there runs.
	if table, err := a.Mounts(); err != nil || !noexec(table, a.Journal) {
		return fmt.Errorf("the worker's journal directory %s is not a noexec mount: the automation unit requires the mount unit that makes it one", a.Journal)
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
	profiles, err := lendByProfile(run, lent)
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
	own := a.beginRun()
	defer own.end()
	return a.work(ctx, image, run, token, profiles, own)
}

// work runs one container per profile, one after another, each until its
// queue is empty, all within the bound, keeping the token current
// meanwhile; a container its client stopped waiting on is removed. One
// that fails leaves the next profile's runs to its own container.
func (a *Automation) work(ctx context.Context, image, run, token string, profiles []string, own ownRun) error {
	runCtx, cancel := context.WithTimeout(ctx, a.Bound)
	defer cancel()
	ended := make(chan struct{})
	defer close(ended)
	if token != "" {
		go a.keepToken(runCtx, ended, run, token)
	}
	var failed []error
	for _, profile := range profiles {
		name := "severino-hq-automation-" + rand.Text()
		args := slices.Insert(AutomationArgs(name, image, run, a.Journal, a.ID, profile, *a.Project), 1, own.label())
		command := a.Command(runCtx, "docker", args...)
		command.Stdout, command.Stderr = a.Stdout, a.Stderr
		err := command.Run()
		if runCtx.Err() != nil {
			stop, done := context.WithTimeout(context.WithoutCancel(ctx), time.Minute)
			_ = a.Command(stop, "docker", "rm", "--force", name).Run()
			done()
			return fmt.Errorf("the automation worker was stopped: %w", runCtx.Err())
		}
		if err != nil {
			failed = append(failed, fmt.Errorf("the %s profile's worker: %w", profile, err))
		}
	}
	return errors.Join(failed...)
}

// lendByProfile moves each lent identity into the ssh directory of its
// connection's privilege profile (profileSSH), beside the pinned host keys,
// and answers the profiles, the read profile's always: it scans the
// playbooks, which takes no login. An identity is moved, so it is in its own
// profile's directory alone.
func lendByProfile(run string, lent []connections.Connection) ([]string, error) {
	profiles := []string{connections.ReadProfile}
	of := map[string]string{}
	for _, connection := range lent {
		profile := connection.SSHTransport.Profile
		of[connection.Ref] = profile
		if !slices.Contains(profiles, profile) {
			profiles = append(profiles, profile)
		}
	}
	slices.Sort(profiles)
	shared := filepath.Join(run, "ssh")
	for _, profile := range profiles {
		dir := profileSSH(run, profile)
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, err
		}
		if err := os.Link(filepath.Join(shared, knownHosts), filepath.Join(dir, knownHosts)); err != nil && !errors.Is(err, fs.ErrNotExist) {
			return nil, err
		}
	}
	entries, err := os.ReadDir(shared)
	if err != nil {
		return nil, err
	}
	for _, entry := range entries {
		if profile, ok := of[identityOf(entry.Name())]; ok {
			if err := os.Rename(filepath.Join(shared, entry.Name()), filepath.Join(profileSSH(run, profile), entry.Name())); err != nil {
				return nil, err
			}
		}
	}
	return profiles, os.RemoveAll(shared)
}

// profileSSH is the ssh directory of one profile's worker in run.
func profileSSH(run, profile string) string { return filepath.Join(run, "profiles", profile, "ssh") }

// noexec is whether the mount on top at dir, in a mount table's fstab(5)
// format, runs no program.
func noexec(table []byte, dir string) bool {
	found := false
	for line := range strings.Lines(string(table)) {
		if fields := strings.Fields(line); len(fields) >= 4 && hostcore.Unescape(fields[1]) == dir {
			found = slices.Contains(strings.Split(fields[3], ","), "noexec")
		}
	}
	return found
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

// AutomationArgs is the container of the worker lent profile's logins: the
// web account, which the bridge's peer check requires, with no capability
// and no way to gain one; a read-only root with only a scratch tmpfs and its
// journal to write, both noexec; its own network, from which the egress table
// lets it reach its runner connections and public addresses alone; public
// resolvers; and of the host's, the lent connections, that profile's
// identities alone and the token, read-only, and its own bridge socket's
// volume, never the controller's.
func AutomationArgs(name, image, run, journal, id, profile string, project AutomationConfig) []string {
	args := []string{
		"run", "--rm", "--pull", "never", "--name", name,
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
		"--mount", readOnly(profileSSH(run, profile), automationSSH),
		"--mount", "type=bind,source="+journal+",target="+automationJournal,
		"--mount", "type=volume,source="+volumeAt(filepath.Dir(AutomationBridgeSocket)).Name+",target="+filepath.Dir(AutomationBridgeSocket)+",readonly",
		"--env", "HQ_AUTOMATION_ID="+id,
		"--env", "HQ_AUTOMATION_PROFILE="+profile,
		"--env", "HQ_AUTOMATION_BRIDGE_SOCKET="+AutomationBridgeSocket,
		// Each profile's worker keeps a journal of its own.
		"--env", "HQ_AUTOMATION_JOURNAL="+automationJournal+"/"+profile+".json",
		"--env", "HQ_AUTOMATION_CONNECTIONS="+automationConnections,
		"--env", "HQ_AUTOMATION_SSH_DIR="+automationSSH,
		"--env", "HQ_AUTOMATION_SCRATCH="+automationScratch,
		"--env", "HQ_AUTOMATION_REPOSITORY="+project.Repository,
	)
	if project.Grant != "" {
		args = append(args,
			"--mount", readOnly(filepath.Join(run, "token"), automationToken),
			"--env", "HQ_AUTOMATION_TOKEN_FILE="+automationToken+"/"+automationTokenName)
	}
	return append(args, "--entrypoint", "python", image, "-m", "hq_runner")
}
