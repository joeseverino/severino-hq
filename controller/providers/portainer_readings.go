package providers

import (
	"context"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"unicode"
	"unicode/utf8"
)

// The readings the Portainer adapter declares: environments, and each Docker
// environment's networks, data mounts, images, container runtime and compose
// projects. One container list per environment feeds them all.

// PortainerEnvironmentRecord is one environment a Portainer connection reaches, up or down.
type PortainerEnvironmentRecord struct {
	ConnectionRef     string  `json:"connection_ref"`
	Host              string  `json:"host"`
	ID                pyValue `json:"id"`
	Name              string  `json:"name"`
	Address           string  `json:"address"`
	Local             bool    `json:"local"`
	Type              string  `json:"type"`
	Status            string  `json:"status"`
	AgentVersion      string  `json:"agent_version"`
	DockerVersion     string  `json:"docker_version"`
	ContainersRunning pyValue `json:"containers_running"`
	ContainersTotal   pyValue `json:"containers_total"`
	SnapshotAt        string  `json:"snapshot_at"`
}

// portainerSite is a reachable environment and the machine it is; every
// per-environment record carries it.
type portainerSite struct {
	ConnectionRef string  `json:"connection_ref"`
	EnvironmentID pyValue `json:"environment_id"`
	Host          string  `json:"host"`
	HostAddress   string  `json:"host_address"`
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
	Tags       []pyValue            `json:"tags"`
	Digests    []pyValue            `json:"digests"`
	CreatedAt  string               `json:"created_at"`
	Size       pyValue              `json:"size"`
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
	ExposedPorts   []int64                 `json:"exposed_ports"`
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

func (r *Registry) portainerListed(ctx context.Context, ref string) ([]PortainerEnvironment, error) {
	environments, err := r.portainerEnvironments(ctx, ref)
	if err != nil {
		return nil, npmRefused(err, "The environment list", portainerNeeds)
	}
	return environments, nil
}

func (r *Registry) portainerEnvironmentReading(ctx context.Context) ([]any, error) {
	local := r.controllerID()
	found := []any{}
	for _, ref := range r.Env.Refs("portainer") {
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
// before failing, as a generator extended into the result keeps its earlier items.
type portainerBuild func(context.Context, portainerSite) ([]any, error)

// portainerEach reads every reachable environment. One that cannot be read is
// that machine refused; every environment refusing is the reading refused.
func (r *Registry) portainerEach(what string, build portainerBuild) Reader {
	return func(ctx context.Context) ([]any, error) {
		local := r.controllerID()
		reachable := []portainerSite{}
		for _, ref := range r.Env.Refs("portainer") {
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
				refusal := npmRefused(err, what, portainerNeeds)
				failures = append(failures, refusal)
				refuseAt(ctx, "", at.ConnectionRef, at.Host, at.HostAddress, refusal)
			}
		}
		if len(reachable) > 0 && len(failures) == len(reachable) {
			return nil, failures[0]
		}
		return found, nil
	}
}

// siteContainers is an environment's containers, never this controller's own run.
func (r *Registry) siteContainers(ctx context.Context, at portainerSite) ([]pyValue, error) {
	listed, err := r.portainerDocker(ctx, at.ConnectionRef, at.EnvironmentID, "/containers/json?all=1")
	if err != nil {
		return nil, err
	}
	found := []pyValue{}
	for _, item := range items(listed.or(pyEmptyList)) {
		if item.object && !r.isThisRun(item) {
			found = append(found, item)
		}
	}
	return found, nil
}

func (r *Registry) siteDocker(ctx context.Context, at portainerSite, path string) (pyValue, error) {
	return r.portainerDocker(ctx, at.ConnectionRef, at.EnvironmentID, path)
}

// keysOf iterates a decoded mapping's keys, or a list's items as text.
func keysOf(value pyValue) []string {
	if value.object {
		return value.keys
	}
	names := []string{}
	for _, item := range items(value) {
		names = append(names, item.str())
	}
	return names
}

func sortedStrings(values []string) []string {
	out := append([]string{}, values...)
	sort.Strings(out)
	return out
}

func (r *Registry) portainerNetworks(ctx context.Context, at portainerSite) ([]any, error) {
	containers, err := r.siteContainers(ctx, at)
	if err != nil {
		return nil, err
	}
	attached := map[string][]string{}
	for _, container := range containers {
		networks := container.get("NetworkSettings").or(pyEmptyObject).get("Networks").or(pyEmptyObject)
		for _, name := range keysOf(networks) {
			attached[name] = append(attached[name], containerName(container))
		}
	}
	listed, err := r.siteDocker(ctx, at, "/networks")
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, network := range items(listed.or(pyEmptyList)) {
		name := strOr(network.get("Name"))
		if name == "" {
			continue
		}
		subnets := []string{}
		for _, entry := range items(network.get("IPAM").or(pyEmptyObject).get("Config").or(pyEmptyList)) {
			if entry.object && entry.get("Subnet").truthy() {
				subnets = append(subnets, entry.get("Subnet").str())
			}
		}
		found = append(found, PortainerNetworkRecord{
			portainerSite: at,
			ID:            strOr(network.get("Id")), Name: name,
			Driver: strOr(network.get("Driver")), Scope: strOr(network.get("Scope")),
			Internal: network.get("Internal").truthy(), Subnets: subnets,
			Containers: sortedStrings(attached[name]),
		})
	}
	return found, nil
}

// mountUsers is each mount of kind by its name (a volume) or source (a bind), and who mounts it.
func mountUsers(containers []pyValue, kind string) (map[string][]PortainerMountUser, []string) {
	users := map[string][]PortainerMountUser{}
	order := []string{}
	for _, container := range containers {
		for _, mount := range items(container.get("Mounts").or(pyEmptyList)) {
			if !mount.get("Type").eqText(kind) {
				continue
			}
			key := mount.get("Source").or(pyText("")).str()
			if kind == "volume" {
				key = mount.get("Name").str()
			}
			if key == "" {
				continue
			}
			if _, seen := users[key]; !seen {
				order = append(order, key)
			}
			users[key] = append(users[key], PortainerMountUser{
				Container:   containerName(container),
				Destination: strOr(mount.get("Destination")),
				ReadOnly:    mount.get("RW").literal == "false",
			})
		}
	}
	return users, order
}

func usersOf(users map[string][]PortainerMountUser, key string) []PortainerMountUser {
	if found, ok := users[key]; ok {
		return found
	}
	return []PortainerMountUser{}
}

func (r *Registry) portainerVolumes(ctx context.Context, at portainerSite) ([]any, error) {
	containers, err := r.siteContainers(ctx, at)
	if err != nil {
		return nil, err
	}
	named, _ := mountUsers(containers, "volume")
	listed, err := r.siteDocker(ctx, at, "/volumes")
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, volume := range items(listed.or(pyEmptyObject).get("Volumes").or(pyEmptyList)) {
		name := strOr(volume.get("Name"))
		if name == "" {
			continue
		}
		found = append(found, PortainerVolumeRecord{
			portainerSite: at, Type: "volume", Name: name,
			Driver: strOr(volume.get("Driver")), Source: strOr(volume.get("Mountpoint")),
			Stack:     strOr(volume.get("Labels").or(pyEmptyObject).get(composeProject)),
			CreatedAt: strOr(volume.get("CreatedAt")), UsedBy: usersOf(named, name),
		})
	}
	binds, sources := mountUsers(containers, "bind")
	sort.Strings(sources)
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
		if imageID := strOr(container.get("ImageID")); imageID != "" {
			running[imageID] = append(running[imageID], PortainerImageUser{
				Container: containerName(container),
				Reference: strOr(container.get("Image")),
				Service:   strOr(container.get("Labels").or(pyEmptyObject).get(composeService)),
			})
		}
	}
	listed, err := r.siteDocker(ctx, at, "/images/json")
	if err != nil {
		return nil, err
	}
	found := []any{}
	for _, image := range items(listed.or(pyEmptyList)) {
		imageID := strOr(image.get("Id"))
		if imageID == "" {
			continue
		}
		tags := []pyValue{}
		for _, tag := range items(image.get("RepoTags").or(pyEmptyList)) {
			if tag.truthy() && !tag.eqText("<none>:<none>") {
				tags = append(tags, tag)
			}
		}
		digests := []pyValue{}
		for _, digest := range items(image.get("RepoDigests").or(pyEmptyList)) {
			if digest.truthy() && !strings.Contains(digest.str(), "<none>") {
				digests = append(digests, digest)
			}
		}
		size := pyValue{}
		if image.get("Size").isInt() {
			size = image.get("Size")
		}
		users := running[imageID]
		if users == nil {
			users = []PortainerImageUser{}
		}
		found = append(found, PortainerImageRecord{
			portainerSite: at, ID: imageID, Tags: tags, Digests: digests,
			CreatedAt: pyStamp(image.get("Created")), Size: size, Containers: users,
		})
	}
	return found, nil
}

// securityOptMax keeps a short option, which names a mode; a longer one can be a whole seccomp profile.
const securityOptMax = 64

func truthyStrings(value pyValue) []string {
	found := []string{}
	for _, item := range items(value.or(pyEmptyList)) {
		if item.truthy() {
			found = append(found, item.str())
		}
	}
	return found
}

func intOf(value pyValue) (int64, error) {
	n, ok := value.or(pyValue{literal: "0"}).toInt()
	if !ok {
		return 0, &ProviderError{Message: "Portainer returned an invalid inspect document."}
	}
	return n, nil
}

// roundTwo is Python's round(x, 2).
func roundTwo(x float64) float64 {
	rounded, _ := strconv.ParseFloat(strconv.FormatFloat(x, 'f', 2, 64), 64)
	return rounded
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if !unicode.IsDigit(c) {
			return false
		}
	}
	return true
}

// portainerInspectLimit bounds the inspects in flight against one Portainer
// site when HQ_PORTAINER_INSPECT_CONCURRENCY does not say. Python reads them
// one at a time; 8 is well under what Docker's API takes, so a large site is
// not the sweep's critical path.
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
	doc pyValue
	err error
}

// inspectContainers inspects each container, inspectLimit at a time.
// The answers are in the order of ids, and the first failure in that order is
// the one the caller reports, as when they were read one by one. After a
// failure no further inspect is started.
func (r *Registry) inspectContainers(ctx context.Context, at portainerSite, ids []string) []containerInspect {
	out := make([]containerInspect, len(ids))
	var failed atomic.Bool
	var wg sync.WaitGroup
	slots := make(chan struct{}, r.inspectLimit())
	for i, id := range ids {
		slots <- struct{}{}
		if failed.Load() {
			<-slots
			break
		}
		wg.Add(1)
		go func() {
			defer func() { <-slots; wg.Done() }()
			out[i].doc, out[i].err = r.siteDocker(ctx, at, "/containers/"+id+"/json")
			if out[i].err != nil {
				failed.Store(true)
			}
		}()
	}
	wg.Wait()
	return out
}

// portainerRuntime is how each container is run, one inspect per container,
// built field by field so the inspect's environment is never copied out.
func (r *Registry) portainerRuntime(ctx context.Context, at portainerSite) ([]any, error) {
	containers, err := r.siteContainers(ctx, at)
	if err != nil {
		return nil, err
	}
	found := []any{}
	none := pyValue{array: []pyValue{pyText("NONE")}}
	identified := []pyValue{}
	identifiers := []string{}
	for _, container := range containers {
		if identifier := strOr(container.get("Id")); identifier != "" {
			identified = append(identified, container)
			identifiers = append(identifiers, identifier)
		}
	}
	inspects := r.inspectContainers(ctx, at, identifiers)
	for i, container := range identified {
		inspect, err := inspects[i].doc, inspects[i].err
		if err != nil {
			return found, err
		}
		inspect = inspect.or(pyEmptyObject)
		config := inspect.get("Config").or(pyEmptyObject)
		host := inspect.get("HostConfig").or(pyEmptyObject)
		state := inspect.get("State").or(pyEmptyObject)
		labels := config.get("Labels").or(pyEmptyObject)

		securityOpt := []string{}
		for _, item := range items(host.get("SecurityOpt").or(pyEmptyList)) {
			if !item.truthy() {
				continue
			}
			text := item.str()
			if utf8.RuneCountInString(text) > securityOptMax {
				text = strings.SplitN(text, "=", 2)[0] + "=(profile)"
			}
			securityOpt = append(securityOpt, text)
		}
		devices := []string{}
		for _, item := range items(host.get("Devices").or(pyEmptyList)) {
			if item.object {
				devices = append(devices, strOr(item.get("PathOnHost")))
			}
		}
		mounts := []PortainerRuntimeMount{}
		for _, item := range items(inspect.get("Mounts").or(pyEmptyList)) {
			if !item.object {
				continue
			}
			source := item.get("Source").or(pyText("")).str()
			if item.get("Type").eqText("volume") {
				source = item.get("Name").str()
			}
			mounts = append(mounts, PortainerRuntimeMount{Type: strOr(item.get("Type")), Source: source, Destination: strOr(item.get("Destination")), ReadOnly: item.get("RW").literal == "false"})
		}
		bindings := []PortainerPortBinding{}
		portBindings := host.get("PortBindings").or(pyEmptyObject)
		for _, port := range sortedStrings(keysOf(portBindings)) {
			for _, bound := range items(portBindings.get(port).or(pyEmptyList)) {
				if bound.object {
					bindings = append(bindings, PortainerPortBinding{ContainerPort: port, HostIP: strOr(bound.get("HostIp")), HostPort: strOr(bound.get("HostPort"))})
				}
			}
		}
		exposed := []int64{}
		for _, port := range keysOf(config.get("ExposedPorts").or(pyEmptyObject)) {
			if head := strings.SplitN(port, "/", 2)[0]; isDigits(head) {
				n, err := strconv.ParseInt(head, 10, 64)
				if err != nil {
					return found, &ProviderError{Message: "Portainer returned an invalid inspect document."}
				}
				exposed = append(exposed, n)
			}
		}
		sort.Slice(exposed, func(a, b int) bool { return exposed[a] < exposed[b] })
		memory, err := intOf(host.get("Memory"))
		if err != nil {
			return found, err
		}
		nanoCPUs, err := intOf(host.get("NanoCpus"))
		if err != nil {
			return found, err
		}
		pids, err := intOf(host.get("PidsLimit"))
		if err != nil {
			return found, err
		}
		restarts, err := intOf(inspect.get("RestartCount"))
		if err != nil {
			return found, err
		}
		test := config.get("Healthcheck").or(pyEmptyObject).get("Test")
		found = append(found, PortainerRuntimeRecord{
			portainerSite:  at,
			Container:      containerName(container),
			Stack:          strOr(labels.get(composeProject)),
			Service:        strOr(labels.get(composeService)),
			ImageID:        strOr(inspect.get("Image")),
			User:           strOr(config.get("User")),
			Privileged:     host.get("Privileged").truthy(),
			ReadOnlyRootfs: host.get("ReadonlyRootfs").truthy(),
			NetworkMode:    strOr(host.get("NetworkMode")),
			PidMode:        strOr(host.get("PidMode")),
			IpcMode:        strOr(host.get("IpcMode")),
			CapAdd:         truthyStrings(host.get("CapAdd")),
			CapDrop:        truthyStrings(host.get("CapDrop")),
			SecurityOpt:    securityOpt,
			Devices:        devices,
			Mounts:         mounts,
			PortBindings:   bindings,
			ExposedPorts:   exposed,
			MemoryLimit:    memory,
			CPULimit:       roundTwo(float64(nanoCPUs) / 1e9),
			PidsLimit:      pids,
			RestartPolicy:  strOr(host.get("RestartPolicy").or(pyEmptyObject).get("Name")),
			Healthcheck:    test.truthy() && !test.eq(none),
			Health:         strOr(state.get("Health").or(pyEmptyObject).get("Status")),
			RestartCount:   restarts,
			StartedAt:      strOr(state.get("StartedAt")),
		})
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
		labels := container.get("Labels").or(pyEmptyObject)
		name := strOr(labels.get(composeProject))
		if name == "" {
			continue
		}
		project := projects[name]
		if project == nil {
			files := []string{}
			for _, part := range strings.Split(strOr(labels.get(composeConfigFiles)), ",") {
				if part = strings.TrimSpace(part); part != "" {
					files = append(files, part)
				}
			}
			project = &PortainerComposeProject{portainerSite: at, Name: name, Source: "compose", WorkingDir: strOr(labels.get(composeWorkingDir)), ConfigFiles: &files}
			projects[name] = project
		}
		project.Containers = append(project.Containers, containerName(container))
	}
	stacks, err := r.portainerStackList(ctx, at.ConnectionRef)
	if err != nil {
		return nil, err
	}
	for _, stack := range stacks {
		if !stack.get("EndpointId").eq(at.EnvironmentID) || !stack.get("Name").truthy() {
			continue
		}
		name := stack.get("Name").str()
		project := projects[name]
		fresh := project == nil
		if fresh {
			project = &PortainerComposeProject{portainerSite: at, Name: name}
			projects[name] = project
		}
		status, _ := pyLookup(portainerStackState, stack.get("Status"))
		entry := strOr(stack.get("EntryPoint"))
		project.Source, project.Status, project.EntryPoint = "portainer", &status, &entry
		if fresh {
			project.WorkingDir = strOr(stack.get("ProjectPath"))
		}
	}
	names := []string{}
	for name := range projects {
		names = append(names, name)
	}
	sort.Strings(names)
	found := []any{}
	for _, name := range names {
		project := *projects[name]
		project.Containers = sortedStrings(project.Containers)
		found = append(found, project)
	}
	return found, nil
}
