package providers

import (
	"context"
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"unicode/utf8"

	"github.com/joeseverino/severino-hq/controller/runtime"
	"golang.org/x/sync/errgroup"
)

// The readings the Portainer adapter declares: environments, and each Docker
// environment's networks, data mounts, images, container runtime and compose
// projects. One container list per environment feeds them all.

// PortainerEnvironmentRecord is one environment a Portainer connection reaches, up or down.
type PortainerEnvironmentRecord struct {
	ConnectionRef     string `json:"connection_ref"`
	Host              string `json:"host"`
	ID                int64  `json:"id"`
	Name              string `json:"name"`
	Address           string `json:"address"`
	Local             bool   `json:"local"`
	Type              string `json:"type"`
	Status            string `json:"status"`
	AgentVersion      string `json:"agent_version"`
	DockerVersion     string `json:"docker_version"`
	ContainersRunning *int64 `json:"containers_running"`
	ContainersTotal   *int64 `json:"containers_total"`
	SnapshotAt        string `json:"snapshot_at"`
}

// portainerSite is a reachable environment and the machine it is; every
// per-environment record carries it.
type portainerSite struct {
	ConnectionRef string `json:"connection_ref"`
	EnvironmentID int64  `json:"environment_id"`
	Host          string `json:"host"`
	HostAddress   string `json:"host_address"`
}

type PortainerNetworkRecord struct {
	portainerSite
	ID         string   `json:"id"`
	Name       string   `json:"name"`
	Driver     string   `json:"driver"`
	Scope      string   `json:"scope"`
	Internal   bool     `json:"internal"`
	Subnets    []string `json:"subnets"`
	Containers []string `json:"containers"`
}

type PortainerMountUser struct {
	Container   string `json:"container"`
	Destination string `json:"destination"`
	ReadOnly    bool   `json:"read_only"`
}

type PortainerVolumeRecord struct {
	portainerSite
	Type      string               `json:"type"`
	Name      string               `json:"name"`
	Driver    string               `json:"driver"`
	Source    string               `json:"source"`
	Stack     string               `json:"stack"`
	CreatedAt string               `json:"created_at"`
	UsedBy    []PortainerMountUser `json:"used_by"`
}

type PortainerBindRecord struct {
	portainerSite
	Type   string               `json:"type"`
	Source string               `json:"source"`
	UsedBy []PortainerMountUser `json:"used_by"`
}

type PortainerImageUser struct {
	Container string `json:"container"`
	Reference string `json:"reference"`
	Service   string `json:"service"`
}

type PortainerImageRecord struct {
	portainerSite
	ID         string               `json:"id"`
	Tags       []string             `json:"tags"`
	Digests    []string             `json:"digests"`
	CreatedAt  string               `json:"created_at"`
	Size       int64                `json:"size"`
	Containers []PortainerImageUser `json:"containers"`
}

type PortainerRuntimeMount struct {
	Type        string `json:"type"`
	Source      string `json:"source"`
	Destination string `json:"destination"`
	ReadOnly    bool   `json:"read_only"`
}

type PortainerPortBinding struct {
	ContainerPort string `json:"container_port"`
	HostIP        string `json:"host_ip"`
	HostPort      string `json:"host_port"`
}

type PortainerRuntimeRecord struct {
	portainerSite
	Container      string                  `json:"container"`
	Stack          string                  `json:"stack"`
	Service        string                  `json:"service"`
	ImageID        string                  `json:"image_id"`
	User           string                  `json:"user"`
	Privileged     bool                    `json:"privileged"`
	ReadOnlyRootfs bool                    `json:"read_only_rootfs"`
	NetworkMode    string                  `json:"network_mode"`
	PidMode        string                  `json:"pid_mode"`
	IpcMode        string                  `json:"ipc_mode"`
	CapAdd         []string                `json:"cap_add"`
	CapDrop        []string                `json:"cap_drop"`
	SecurityOpt    []string                `json:"security_opt"`
	Devices        []string                `json:"devices"`
	Mounts         []PortainerRuntimeMount `json:"mounts"`
	PortBindings   []PortainerPortBinding  `json:"port_bindings"`
	ExposedPorts   []int                   `json:"exposed_ports"`
	MemoryLimit    int64                   `json:"memory_limit"`
	CPULimit       float64                 `json:"cpu_limit"`
	PidsLimit      int64                   `json:"pids_limit"`
	RestartPolicy  string                  `json:"restart_policy"`
	Healthcheck    bool                    `json:"healthcheck"`
	Health         string                  `json:"health"`
	RestartCount   int64                   `json:"restart_count"`
	StartedAt      string                  `json:"started_at"`
}

// PortainerComposeProject is a compose project seen in container labels, a
// Portainer stack, or both. Only a compose project knows its config files; only
// a Portainer stack has a status and entry point.
type PortainerComposeProject struct {
	portainerSite
	Name        string    `json:"name"`
	Source      string    `json:"source"`
	WorkingDir  string    `json:"working_dir"`
	ConfigFiles *[]string `json:"config_files,omitempty"`
	Status      *string   `json:"status,omitempty"`
	EntryPoint  *string   `json:"entry_point,omitempty"`
	Containers  []string  `json:"containers"`
}

// portainerRefused names the read that failed and, for a refusal, what it needs.
func portainerRefused(err error, what string) error {
	switch failure, _, _ := runtime.Classify(err); failure {
	case runtime.FailureClassCredential:
		return &ProviderError{Message: what + ": credential refused", Failure: failure, Err: err}
	case runtime.FailureClassPermission:
		return &ProviderError{Message: what + " needs " + portainerNeeds, Failure: failure, Err: err}
	}
	return fmt.Errorf("%s: %w", what, err)
}

func (r *Registry) portainerListed(ctx context.Context, ref string) ([]PortainerEnvironment, error) {
	environments, err := r.portainerEnvironments(ctx, ref)
	if err != nil {
		return nil, portainerRefused(err, "environment list")
	}
	return environments, nil
}

func (r *Registry) portainerEnvironmentReading(ctx context.Context) ([]any, error) {
	local := r.controllerID()
	found := []any{}
	for _, ref := range r.Env.Refs(runtime.ConnectionProviderPortainer) {
		environments, err := r.portainerListed(ctx, ref)
		if err != nil {
			return nil, err
		}
		for _, item := range environments {
			found = append(found, PortainerEnvironmentRecord{
				ConnectionRef: ref, Host: machineName(item, local), ID: item.ID, Name: item.Name,
				Address: item.Address, Local: item.Local, Type: item.Type, Status: item.Status,
				AgentVersion: item.AgentVersion, DockerVersion: item.DockerVersion,
				ContainersRunning: item.ContainersRunning, ContainersTotal: item.ContainersTotal, SnapshotAt: item.SnapshotAt,
			})
		}
	}
	return found, nil
}

// portainerBuild reads one environment's records. It returns what it read
// before failing, with the failure.
type portainerBuild func(context.Context, portainerSite) ([]any, error)

// portainerEach reads every reachable environment. One that cannot be read is
// that machine refused; every environment refusing is the reading refused.
func (r *Registry) portainerEach(what string, build portainerBuild) Reader {
	return func(ctx context.Context) ([]any, error) {
		local := r.controllerID()
		reachable := []portainerSite{}
		for _, ref := range r.Env.Refs(runtime.ConnectionProviderPortainer) {
			environments, err := r.portainerListed(ctx, ref)
			if err != nil {
				return nil, err
			}
			for _, item := range environments {
				if item.Reachable {
					reachable = append(reachable, portainerSite{ConnectionRef: ref, EnvironmentID: item.ID, Host: machineName(item, local), HostAddress: item.Address})
				}
			}
		}
		found := []any{}
		var failures []error
		for _, at := range reachable {
			records, err := build(ctx, at)
			found = append(found, records...)
			if err != nil {
				refusal := portainerRefused(err, what)
				failures = append(failures, refusal)
				refuseAt(ctx, runtime.PartWhole, at.ConnectionRef, at.Host, at.HostAddress, refusal)
			}
		}
		if len(reachable) > 0 && len(failures) == len(reachable) {
			return nil, failures[0]
		}
		return found, nil
	}
}

// siteContainers is an environment's containers, never this controller's own run.
func (r *Registry) siteContainers(ctx context.Context, at portainerSite) ([]dockerContainer, error) {
	listed, err := r.portainerContainers(ctx, at.ConnectionRef, at.EnvironmentID)
	if err != nil {
		return nil, err
	}
	found := []dockerContainer{}
	for _, container := range listed {
		if !r.isThisRun(container.Labels) {
			found = append(found, container)
		}
	}
	return found, nil
}

func sortedStrings(values []string) []string {
	out := append([]string{}, values...)
	slices.Sort(out)
	return out
}

// nonEmpty is the values that are not blank.
func nonEmpty(values []string) []string {
	found := []string{}
	for _, value := range values {
		if value != "" {
			found = append(found, value)
		}
	}
	return found
}

func (r *Registry) portainerNetworks(ctx context.Context, at portainerSite) ([]any, error) {
	containers, err := r.siteContainers(ctx, at)
	if err != nil {
		return nil, err
	}
	attached := map[string][]string{}
	for _, container := range containers {
		for name := range container.NetworkSettings.Networks {
			attached[name] = append(attached[name], container.name())
		}
	}
	listed, err := portainerDocker[[]dockerNetwork](ctx, r, at.ConnectionRef, at.EnvironmentID, "/networks", "network list")
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, network := range listed {
		if network.Name == "" {
			continue
		}
		subnets := []string{}
		for _, entry := range network.IPAM.Config {
			if entry.Subnet != "" {
				subnets = append(subnets, entry.Subnet)
			}
		}
		found = append(found, PortainerNetworkRecord{
			portainerSite: at, ID: network.ID, Name: network.Name,
			Driver: network.Driver, Scope: network.Scope, Internal: network.Internal, Subnets: subnets,
			Containers: sortedStrings(attached[network.Name]),
		})
	}
	return found, nil
}

// mountUsers is each mount of one type by its key, who mounts it, and the keys
// in the order first seen.
func mountUsers(containers []dockerContainer, kind string) (map[string][]PortainerMountUser, []string) {
	users := map[string][]PortainerMountUser{}
	order := []string{}
	for _, container := range containers {
		for _, mount := range container.Mounts {
			key := mount.key()
			if mount.Type != kind || key == "" {
				continue
			}
			if _, seen := users[key]; !seen {
				order = append(order, key)
			}
			users[key] = append(users[key], PortainerMountUser{Container: container.name(), Destination: mount.Destination, ReadOnly: mount.readOnly()})
		}
	}
	return users, order
}

func (r *Registry) portainerVolumes(ctx context.Context, at portainerSite) ([]any, error) {
	containers, err := r.siteContainers(ctx, at)
	if err != nil {
		return nil, err
	}
	named, _ := mountUsers(containers, "volume")
	listed, err := portainerDocker[dockerVolumes](ctx, r, at.ConnectionRef, at.EnvironmentID, "/volumes", "volume list")
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, volume := range listed.Volumes {
		if volume.Name == "" {
			continue
		}
		users := named[volume.Name]
		if users == nil {
			users = []PortainerMountUser{}
		}
		found = append(found, PortainerVolumeRecord{
			portainerSite: at, Type: "volume", Name: volume.Name, Driver: volume.Driver, Source: volume.Mountpoint,
			Stack: volume.Labels[composeProject], CreatedAt: volume.CreatedAt, UsedBy: users,
		})
	}
	binds, sources := mountUsers(containers, "bind")
	slices.Sort(sources)
	for _, source := range sources {
		found = append(found, PortainerBindRecord{portainerSite: at, Type: "bind", Source: source, UsedBy: binds[source]})
	}
	return found, nil
}

func (r *Registry) portainerImages(ctx context.Context, at portainerSite) ([]any, error) {
	containers, err := r.siteContainers(ctx, at)
	if err != nil {
		return nil, err
	}
	running := map[string][]PortainerImageUser{}
	for _, container := range containers {
		if container.ImageID != "" {
			running[container.ImageID] = append(running[container.ImageID], PortainerImageUser{
				Container: container.name(), Reference: container.Image, Service: container.Labels[composeService],
			})
		}
	}
	listed, err := portainerDocker[[]dockerImage](ctx, r, at.ConnectionRef, at.EnvironmentID, "/images/json", "image list")
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, image := range listed {
		if image.ID == "" {
			continue
		}
		tags := slices.DeleteFunc(nonEmpty(image.RepoTags), func(tag string) bool { return tag == "<none>:<none>" })
		digests := slices.DeleteFunc(nonEmpty(image.RepoDigests), func(digest string) bool { return strings.Contains(digest, "<none>") })
		users := running[image.ID]
		if users == nil {
			users = []PortainerImageUser{}
		}
		found = append(found, PortainerImageRecord{
			portainerSite: at, ID: image.ID, Tags: tags, Digests: digests,
			CreatedAt: unixStamp(image.Created), Size: image.Size, Containers: users,
		})
	}
	return found, nil
}

// securityOptMax keeps a short option, which names a mode; a longer one can be
// a whole seccomp profile, which is never copied out.
const securityOptMax = 64

// portainerInspectLimit bounds the inspects in flight against one Portainer
// site when HQ_PORTAINER_INSPECT_CONCURRENCY does not say; 8 is well under what
// Docker's API takes, so a large site is not the sweep's critical path.
const (
	portainerInspectLimit    = 8
	portainerInspectMaxLimit = 64
	inspectConcurrencyEnv    = "HQ_PORTAINER_INSPECT_CONCURRENCY"
)

// inspectLimit is the operator's inspect concurrency, clamped to 1..64.
func (r *Registry) inspectLimit() int {
	limit, err := strconv.Atoi(strings.TrimSpace(r.Env[inspectConcurrencyEnv]))
	if err != nil {
		return portainerInspectLimit
	}
	return min(max(limit, 1), portainerInspectMaxLimit)
}

type containerInspect struct {
	doc dockerInspect
	err error
}

// inspectContainers inspects each container, inspectLimit at a time.
// The answers are in the order of ids, and the first failure in that order is
// the one the caller reports. After a failure no further inspect is started.
func (r *Registry) inspectContainers(ctx context.Context, at portainerSite, ids []string) []containerInspect {
	out := make([]containerInspect, len(ids))
	var failed atomic.Bool
	var group errgroup.Group
	group.SetLimit(r.inspectLimit())
	for i, id := range ids {
		if failed.Load() {
			break
		}
		// Go waits for a free slot; a failure that freed it starts nothing.
		group.Go(func() error {
			if failed.Load() {
				return nil
			}
			out[i].doc, out[i].err = portainerDocker[dockerInspect](ctx, r, at.ConnectionRef, at.EnvironmentID, "/containers/"+id+"/json", "container inspect")
			if out[i].err != nil {
				failed.Store(true)
			}
			return nil
		})
	}
	group.Wait()
	return out
}

// runtimeRecord is how one container is run, built field by field so the
// inspect's environment is never copied out.
func runtimeRecord(at portainerSite, container dockerContainer, inspect dockerInspect) PortainerRuntimeRecord {
	config, host := inspect.Config, inspect.HostConfig
	securityOpt := []string{}
	for _, option := range nonEmpty(host.SecurityOpt) {
		if utf8.RuneCountInString(option) > securityOptMax {
			name, _, _ := strings.Cut(option, "=")
			option = name + "=(profile)"
		}
		securityOpt = append(securityOpt, option)
	}
	devices := []string{}
	for _, device := range host.Devices {
		devices = append(devices, device.PathOnHost)
	}
	mounts := []PortainerRuntimeMount{}
	for _, mount := range inspect.Mounts {
		mounts = append(mounts, PortainerRuntimeMount{Type: mount.Type, Source: mount.key(), Destination: mount.Destination, ReadOnly: mount.readOnly()})
	}
	bindings := []PortainerPortBinding{}
	for _, port := range sortedStrings(mapKeys(host.PortBindings)) {
		for _, bound := range host.PortBindings[port] {
			bindings = append(bindings, PortainerPortBinding{ContainerPort: port, HostIP: bound.HostIP, HostPort: bound.HostPort})
		}
	}
	exposed := []int{}
	for port := range config.ExposedPorts {
		number, _, _ := strings.Cut(port, "/")
		if n, err := strconv.Atoi(number); err == nil && n > 0 {
			exposed = append(exposed, n)
		}
	}
	slices.Sort(exposed)
	healthcheck := false
	if check := config.Healthcheck; check != nil {
		healthcheck = len(check.Test) > 0 && !slices.Equal(check.Test, []string{"NONE"})
	}
	health := ""
	if inspect.State.Health != nil {
		health = inspect.State.Health.Status
	}
	pids := int64(0)
	if host.PidsLimit != nil {
		pids = *host.PidsLimit
	}
	return PortainerRuntimeRecord{
		portainerSite:  at,
		Container:      container.name(),
		Stack:          config.Labels[composeProject],
		Service:        config.Labels[composeService],
		ImageID:        inspect.Image,
		User:           config.User,
		Privileged:     host.Privileged,
		ReadOnlyRootfs: host.ReadonlyRootfs,
		NetworkMode:    host.NetworkMode,
		PidMode:        host.PidMode,
		IpcMode:        host.IpcMode,
		CapAdd:         nonEmpty(host.CapAdd),
		CapDrop:        nonEmpty(host.CapDrop),
		SecurityOpt:    securityOpt,
		Devices:        devices,
		Mounts:         mounts,
		PortBindings:   bindings,
		ExposedPorts:   exposed,
		MemoryLimit:    host.Memory,
		CPULimit:       math.Round(float64(host.NanoCpus)/1e7) / 100,
		PidsLimit:      pids,
		RestartPolicy:  host.RestartPolicy.Name,
		Healthcheck:    healthcheck,
		Health:         health,
		RestartCount:   inspect.RestartCount,
		StartedAt:      inspect.State.StartedAt,
	}
}

func mapKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for key := range m {
		keys = append(keys, key)
	}
	return keys
}

// portainerRuntime is how each container is run, one inspect per container.
func (r *Registry) portainerRuntime(ctx context.Context, at portainerSite) ([]any, error) {
	containers, err := r.siteContainers(ctx, at)
	if err != nil {
		return nil, err
	}
	identified := slices.DeleteFunc(containers, func(c dockerContainer) bool { return c.ID == "" })
	ids := make([]string, len(identified))
	for i, container := range identified {
		ids[i] = container.ID
	}
	inspects := r.inspectContainers(ctx, at, ids)
	found := []any{}
	for i, container := range identified {
		if err := inspects[i].err; err != nil {
			return found, err
		}
		found = append(found, runtimeRecord(at, container, inspects[i].doc))
	}
	return found, nil
}

func (r *Registry) portainerStacksReading(ctx context.Context, at portainerSite) ([]any, error) {
	containers, err := r.siteContainers(ctx, at)
	if err != nil {
		return nil, err
	}
	projects := map[string]*PortainerComposeProject{}
	for _, container := range containers {
		name := container.Labels[composeProject]
		if name == "" {
			continue
		}
		project := projects[name]
		if project == nil {
			files := []string{}
			for part := range strings.SplitSeq(container.Labels[composeConfigFiles], ",") {
				if part = strings.TrimSpace(part); part != "" {
					files = append(files, part)
				}
			}
			project = &PortainerComposeProject{portainerSite: at, Name: name, Source: "compose", WorkingDir: container.Labels[composeWorkingDir], ConfigFiles: &files}
			projects[name] = project
		}
		project.Containers = append(project.Containers, container.name())
	}
	stacks, err := r.portainerStacksOn(ctx, at.ConnectionRef, at.EnvironmentID, "")
	if err != nil {
		return nil, err
	}
	for _, stack := range stacks {
		if stack.Name == "" {
			continue
		}
		project := projects[stack.Name]
		if project == nil {
			project = &PortainerComposeProject{portainerSite: at, Name: stack.Name, WorkingDir: stack.ProjectPath}
			projects[stack.Name] = project
		}
		status, entry := portainerStackState[stack.Status], stack.EntryPoint
		project.Source, project.Status, project.EntryPoint = "portainer", &status, &entry
	}
	found := []any{}
	for _, name := range sortedStrings(mapKeys(projects)) {
		project := *projects[name]
		project.Containers = sortedStrings(project.Containers)
		found = append(found, project)
	}
	return found, nil
}
