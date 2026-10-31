package unit

import (
	"context"
	"encoding/json"
	"path/filepath"
	"slices"
	"strings"
)

// options is what a docker command's flag parser knows of its options: the
// ones it reads a value after, and the ones that stand alone. An option in
// neither is one this package cannot step over, so a command holding it
// names no container.
type options struct{ values, flags []string }

var (
	dockerOptions = options{
		values: []string{"-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey"},
		flags:  []string{"-D", "--debug", "--tls", "--tlsverify"},
	}
	startOptions = options{
		values: []string{"--checkpoint", "--checkpoint-dir", "--detach-keys"},
		flags:  []string{"-a", "--attach", "-i", "--interactive"},
	}
	stopOptions = options{values: []string{"-s", "--signal", "-t", "--timeout", "--time"}}
	// runOptions are the options of docker run and create that stand alone;
	// the many that take a value are not listed, since --name is read only
	// before any of them.
	runOptions = options{flags: []string{
		"-d", "--detach", "-i", "--interactive", "-t", "--tty", "--rm", "--init",
		"--privileged", "--read-only", "-P", "--publish-all", "-q", "--quiet",
	}}
	// composeOptions leave out --compatibility, which changes how compose
	// names a container.
	composeOptions = options{
		values: []string{"-f", "--file", "-p", "--project-name", "--project-directory", "--env-file", "--profile", "--ansi", "--progress", "--parallel"},
		flags:  []string{"--dry-run", "--all-resources"},
	}
)

// composeStarts are the compose commands that bring a project's containers
// up.
var composeStarts = []string{"up", "create", "start", "restart"}

// operands are args' operands as docker's flag parser reads them, where an
// option's value is never an operand. Not interspersed, they are everything
// from the first operand on. ok is false at an option the table does not
// hold.
func (o options) operands(args []string, interspersed bool) (operands []string, ok bool) {
	for i := 0; i < len(args); i++ {
		arg := args[i]
		switch {
		case arg == "--":
			return append(operands, args[i+1:]...), true
		case strings.HasPrefix(arg, "--"):
			name, _, inline := strings.Cut(arg, "=")
			switch {
			case slices.Contains(o.values, name):
				if !inline {
					i++
					if i == len(args) {
						return nil, false
					}
				}
			case !slices.Contains(o.flags, name):
				return nil, false
			}
		case strings.HasPrefix(arg, "-") && len(arg) > 1:
			// Short options cluster; one that takes a value takes the rest of
			// the cluster, or the next argument when it ends the cluster.
			for j := 1; j < len(arg); j++ {
				short := "-" + arg[j:j+1]
				if slices.Contains(o.values, short) {
					if j == len(arg)-1 {
						i++
						if i == len(args) {
							return nil, false
						}
					}
					break
				}
				if !slices.Contains(o.flags, short) {
					return nil, false
				}
			}
		case !interspersed:
			return append(operands, args[i:]...), true
		default:
			operands = append(operands, arg)
		}
	}
	return operands, true
}

// starter says whether one start command runs docker on a container of the
// name given.
type starter func(container string) bool

func nothing(string) bool { return false }

// starter is how one of the unit's start commands names the containers it
// runs docker on, read only where docker itself reads a container's name:
// the operands of start, stop and restart, the --name of run and create, and
// the names compose gives a project's services. No argument counts merely
// for equaling a container's name or its directory, since the container's
// creator chose both.
func (r *Runner) starter(ctx context.Context, argv []string) starter {
	if len(argv) < 2 || filepath.Base(argv[0]) != "docker" {
		return nothing
	}
	command, ok := dockerOptions.operands(argv[1:], false)
	if !ok || len(command) == 0 {
		return nothing
	}
	if command[0] == "compose" {
		return r.composed(ctx, command[1:])
	}
	if command[0] == "container" {
		command = command[1:]
	}
	var named []string
	switch {
	case len(command) == 0:
	case command[0] == "start":
		named, ok = startOptions.operands(command[1:], true)
	case command[0] == "stop" || command[0] == "restart":
		named, ok = stopOptions.operands(command[1:], true)
	case command[0] == "run" || command[0] == "create":
		named = runName(command[1:])
	}
	if !ok {
		return nothing
	}
	return func(container string) bool { return slices.Contains(named, container) }
}

// runName is the --name docker run or create gives its container, read only
// while every option before it stands on its own, since past any other the
// next argument may be its value or the image.
func runName(args []string) []string {
	for i, arg := range args {
		name, value, inline := strings.Cut(arg, "=")
		switch {
		case name == "--name" && inline:
			return []string{value}
		case name == "--name" && i+1 < len(args):
			return []string{args[i+1]}
		case strings.HasPrefix(arg, "--") && inline:
		case strings.HasPrefix(arg, "-") && arg != "--":
			if _, ok := runOptions.operands([]string{arg}, false); !ok {
				return nil
			}
		default:
			return nil
		}
	}
	return nil
}

// project is what `docker compose config --format json` prints of the
// project's name and its services' container names.
type project struct {
	Name     string `json:"name"`
	Services map[string]struct {
		ContainerName string `json:"container_name"`
	} `json:"services"`
}

// composed is the containers a compose command brings up, named as compose
// names them from the project's own files, which docker reads with the
// command's own project options: a service's container_name, or
// project-service-N without one. The services the command names, where it
// names any, are the only ones.
func (r *Runner) composed(ctx context.Context, args []string) starter {
	rest, ok := composeOptions.operands(args, false)
	if !ok || len(rest) == 0 || !slices.Contains(composeStarts, rest[0]) {
		return nothing
	}
	global := args[:len(args)-len(rest)]
	if slices.Contains(global, "--") {
		return nothing
	}
	read := append(append([]string{"compose"}, global...), "config", "--format", "json")
	out, err := r.Command(ctx, "docker", read...).Output()
	var config project
	if err != nil || json.Unmarshal(out, &config) != nil || config.Name == "" {
		return nothing
	}
	services := []string{}
	for _, arg := range rest[1:] {
		if _, ok := config.Services[arg]; ok {
			services = append(services, arg)
		}
	}
	if len(services) == 0 {
		for service := range config.Services {
			services = append(services, service)
		}
	}
	return func(container string) bool {
		for _, service := range services {
			if fixed := config.Services[service].ContainerName; fixed != "" {
				if container == fixed {
					return true
				}
				continue
			}
			index, ok := strings.CutPrefix(container, config.Name+"-"+service+"-")
			if ok && index != "" && strings.Trim(index, "0123456789") == "" {
				return true
			}
		}
		return false
	}
}
