package host

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"github.com/joeseverino/severino-hq/controller/internal/hostcore"
)

// The host's own setup is the playbook SetupPlaybook, which the tree carries,
// run by the Ansible of the runner image the release pins (AutomationImage).
// Applied, it installs the release's units and tmpfiles rule, renders what
// host.json decides, and enables and starts the units; checked, it says what
// an apply would change, which is the unit half of verify.
//
// Its security properties:
//   - What runs is signed: the playbook came in the composition the deploy
//     verified and Sync sealed into the root-owned tree, and its engine is the
//     runner digest that composition pins, verified by the same cosign and
//     identity before it was pulled (pullVerified). Each container runs it by
//     digest with --pull never, and its own side has no network, so nothing is
//     fetched.
//   - An apply reaches the host through community.docker.nsenter, so each
//     module runs as root in the host's namespaces on the host's own python3,
//     systemctl and visudo: the reach of an install, which writes units and
//     starts them and so is root by its nature.
//   - A check has no capability, no network and a read-only root, and is lent
//     only the tree and the directories the playbook writes, read-only and at
//     their own paths. So verify and the rehearsal never act on the host.
//   - host.json is read here, by ReadConfig, and only what it validated
//     reaches the playbook, in a file of root's that the playbook reads through
//     a lookup: Ansible renders a template held in an extra var or a vars file,
//     and never in a lookup's text, so a value holding a template is written
//     as it is.
//   - An apply's output goes to the private host log and a check's to its
//     caller, since a drop-in names the vault and the deploy's log is public.
const (
	SetupPlaybook = "deploy/ansible/host.yml"
	// setupBound is the longest a run of the playbook takes before its
	// container is removed: the render it starts and the preflights after it.
	setupBound = 15 * time.Minute
	// setupTmp is where an apply's modules stage themselves on the host.
	setupTmp = "/run/severino-hq-setup"
)

// setupVars are the playbook's variables, each declared here once.
type setupVars struct {
	Tree           string   `json:"tree"`
	SystemdDir     string   `json:"systemd_dir"`
	TmpfilesDir    string   `json:"tmpfiles_dir"`
	SudoersDir     string   `json:"sudoers_dir"`
	Shipped        []string `json:"shipped"`
	UnitPatterns   []string `json:"unit_patterns"`
	SecretsUnit    string   `json:"secrets_unit"`
	WebUnit        string   `json:"web_unit"`
	AutomationUnit string   `json:"automation_unit"`
	HostProgram    string   `json:"host_program"`
	Container      string   `json:"container"`
	Host           hostVars `json:"host"`
}

// hostVars is what the playbook renders of host.json.
type hostVars struct {
	Vault      string        `json:"vault"`
	EnvItem    string        `json:"env_item"`
	Connect    ConnectConfig `json:"connect"`
	Runner     RunnerConfig  `json:"runner"`
	Automation bool          `json:"automation"`
}

// setupVars writes the variables for the playbook in tree, from the host's
// configuration, which must be readable and valid, to a file of its own in a
// directory of root's beneath Runtime, which its caller removes.
func (m *Machine) setupVars(tree string) (string, error) {
	data, err := os.ReadFile(m.HostConfig)
	if err != nil {
		return "", fmt.Errorf("the host configuration is not readable: %w", err)
	}
	config, err := ReadConfig(data)
	if err != nil {
		return "", err
	}
	shipped, err := Shipped(filepath.Join(tree, "deploy", "systemd"))
	if err != nil {
		return "", err
	}
	patterns := make([]string, len(unitSuffixes))
	for i, suffix := range unitSuffixes {
		patterns[i] = "severino-hq*" + suffix
	}
	vars, err := json.Marshal(setupVars{
		Tree: tree, SystemdDir: m.Systemd, TmpfilesDir: m.Tmpfiles, SudoersDir: m.Sudoers,
		Shipped: shipped, UnitPatterns: patterns,
		SecretsUnit: SecretsUnit, WebUnit: hostcore.WebUnit, AutomationUnit: AutomationUnit,
		HostProgram: HostProgram, Container: hostcore.Container,
		Host: hostVars{
			Vault: config.Vault, EnvItem: config.EnvItem, Connect: config.Connect, Runner: config.Runner,
			Automation: config.Automation != nil,
		},
	})
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(m.Runtime, 0o755); err != nil {
		return "", err
	}
	dir, err := os.MkdirTemp(m.Runtime, "severino-hq-setup.")
	if err != nil {
		return "", err
	}
	path := filepath.Join(dir, "setup.json")
	if err := os.WriteFile(path, vars, 0o600); err != nil {
		return "", errors.Join(err, os.RemoveAll(dir))
	}
	return path, nil
}

// pinnedRunner is the runner image the tree at tree pins.
func pinnedRunner(tree string) (string, error) {
	pin, err := os.ReadFile(filepath.Join(tree, AutomationImage))
	if err != nil {
		return "", fmt.Errorf("the tree pins no runner image, which runs its setup: %w", err)
	}
	return Pinned(strings.TrimSpace(string(pin)))
}

// Setup applies the playbook in tree with image, tags narrowing it, its output
// in the private log. Its container acts on the host as root.
func (m *Machine) Setup(ctx context.Context, image, tree string, tags ...string) error {
	label, play := "Host setup", []string{"--connection", "community.docker.nsenter"}
	if len(tags) > 0 {
		label += " (" + strings.Join(tags, ", ") + ")"
		play = append(play, "--tags", strings.Join(tags, ","))
	}
	playbook := filepath.Join(tree, filepath.Dir(SetupPlaybook))
	confine := []string{"--privileged", "--pid", "host", "--mount", readOnly(playbook, playbook), "--env", "ANSIBLE_REMOTE_TMP=" + setupTmp}
	return m.setupRun(ctx, image, tree, confine, play, func(ctx context.Context, args []string) error {
		return m.private(ctx, label, "setup", "docker", args...)
	})
}

// CheckSetup runs the playbook in tree in check mode, in a container that reads
// and cannot write, writing what Ansible's callback prints to out.
func (m *Machine) CheckSetup(ctx context.Context, tree, callback string, out io.Writer) error {
	image, err := pinnedRunner(tree)
	if err != nil {
		return err
	}
	confine := []string{"--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
		"--env", "ANSIBLE_REMOTE_TMP=/tmp/ansible/remote", "--env", "ANSIBLE_STDOUT_CALLBACK=" + callback, "--env", "ANSIBLE_JSON_INDENT=0"}
	for _, dir := range []string{tree, m.Systemd, m.Tmpfiles, m.Sudoers} {
		confine = append(confine, "--mount", readOnly(dir, dir))
	}
	play := []string{"--connection", "local", "--check", "--diff", "--tags", "files"}
	return m.setupRun(ctx, image, tree, confine, play, func(ctx context.Context, args []string) error {
		var said bytes.Buffer
		command := m.Command(ctx, "docker", args...)
		command.Stdout, command.Stderr = out, &said
		if err := command.Run(); err != nil {
			return fmt.Errorf("the setup's check failed: %w: %s", err, strings.TrimSpace(said.String()))
		}
		return nil
	})
}

// setupRun runs the playbook in tree with image as one run of HQ's, confined
// as confine says, its variables file lent read-only at its own path. The
// container is removed at setupBound or when its caller stops waiting.
func (m *Machine) setupRun(ctx context.Context, image, tree string, confine, play []string, run func(context.Context, []string) error) error {
	vars, err := m.setupVars(tree)
	if err != nil {
		return err
	}
	defer os.RemoveAll(filepath.Dir(vars))
	own := m.beginRun()
	defer own.end()
	name := "severino-hq-setup-" + strings.ToLower(own.nonce)
	ctx, cancel := context.WithTimeout(ctx, setupBound)
	defer cancel()
	defer func() { _ = m.run(context.WithoutCancel(ctx), "docker", "rm", "--force", name) }()
	args := append([]string{"run", "--rm", "--pull", "never", "--name", name, own.label(),
		"--user", "0", "--network", "none", "--read-only", "--tmpfs", "/tmp",
		"--mount", readOnly(filepath.Dir(vars), filepath.Dir(vars)),
		"--env", "ANSIBLE_CONFIG=" + filepath.Join(tree, filepath.Dir(SetupPlaybook), "ansible.cfg"),
		"--env", "ANSIBLE_HOME=/tmp/ansible", "--env", "ANSIBLE_LOCAL_TEMP=/tmp/ansible/local"}, confine...)
	args = append(append(args, "--entrypoint", "ansible-playbook", image, "--inventory", "host,", "--extra-vars", "setup_vars="+vars), play...)
	return run(ctx, append(args, filepath.Join(tree, SetupPlaybook)))
}

// SetupDrift is what an apply of the playbook in tree would change on this
// host: one line for each file, by the task that writes it.
func (m *Machine) SetupDrift(ctx context.Context, tree string) ([]string, error) {
	var report bytes.Buffer
	if err := m.CheckSetup(ctx, tree, "ansible.posix.json", &report); err != nil {
		return nil, err
	}
	return setupChanges(report.Bytes())
}

// setupResult is one task's result on the host, as ansible.posix.json writes
// it: a loop's results each carry their own label.
type setupResult struct {
	Changed bool   `json:"changed"`
	Dest    string `json:"dest"`
	Path    string `json:"path"`
	Results []struct {
		Changed bool   `json:"changed"`
		Label   string `json:"_ansible_item_label"`
	} `json:"results"`
}

// setupChanges is each change a check's report names, as "task: path".
func setupChanges(report []byte) ([]string, error) {
	var read struct {
		Plays []struct {
			Tasks []struct {
				Task struct {
					Name string `json:"name"`
				} `json:"task"`
				Hosts map[string]setupResult `json:"hosts"`
			} `json:"tasks"`
		} `json:"plays"`
	}
	if err := json.Unmarshal(report, &read); err != nil {
		return nil, errors.New("the setup's check wrote no report")
	}
	var changes []string
	for _, play := range read.Plays {
		for _, task := range play.Tasks {
			for _, result := range task.Hosts {
				for _, item := range result.Results {
					if item.Changed {
						changes = append(changes, task.Task.Name+": "+item.Label)
					}
				}
				if result.Changed && len(result.Results) == 0 {
					changes = append(changes, task.Task.Name+": "+result.Dest+result.Path)
				}
			}
		}
	}
	slices.Sort(changes)
	return changes, nil
}
