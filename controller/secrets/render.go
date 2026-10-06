// Package secrets is the root renderer: it reads one 1Password vault through
// the Connect server on this machine and installs what the host's consumers
// read. It holds the only reader token; no application and no container
// talks to Connect.
package secrets

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"time"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/secrets/connect"
	"github.com/joeseverino/severino-hq/controller/secrets/connectapi"
	"github.com/joeseverino/severino-hq/controller/secrets/install"
	"github.com/joeseverino/severino-hq/controller/secrets/project"
	"github.com/joeseverino/severino-hq/controller/secretstatus"
)

// Config is the host's configuration, from the unit and its drop-in.
type Config struct {
	// Vault is the vault's name or identifier; EnvItem the application
	// environment item's title or identifier. Both are host configuration.
	Vault, EnvItem string
	// Endpoint is Connect, on IPv4 loopback.
	Endpoint string
	// CredentialsDir is systemd's CREDENTIALS_DIRECTORY; ConnectCredential the
	// name of the reader token in it, one per consumer.
	CredentialsDir, ConnectCredential string
	Layout                            install.Layout
	// RegistryPath is hq/config/controller-connections.json in the root-owned tree.
	RegistryPath string
	// MinAppVariables is the fewest variables a real application environment has.
	MinAppVariables int
	// ConnectTimeout bounds readiness and the whole read of the vault.
	ConnectTimeout time.Duration
	// FullEvery is the longest an unchanged vault goes without a full read.
	FullEvery time.Duration
	// HealthAttempts and HealthInterval bound the wait for a restarted web container.
	HealthAttempts int
	HealthInterval time.Duration
}

// ErrConfig is a configuration the renderer refuses; ErrUnhealthy a web
// container that did not come back; ErrChanging a vault that kept changing
// under the read.
var (
	ErrConfig    = errors.New("the configuration was refused")
	ErrUnhealthy = errors.New("Severino HQ did not become healthy after secret rotation.")
	ErrChanging  = errors.New("the vault kept changing while it was read")
)

type configError struct{ message string }

func (e *configError) Error() string { return e.message }
func (e *configError) Unwrap() error { return ErrConfig }
func errConfig(message string) error { return &configError{message: message} }

// Class is the one word a failure is filed under: in the journal, in the
// status document, and as the exit code (see cmd/hq-secrets).
func Class(err error) string {
	switch {
	case err == nil:
		return ""
	case errors.Is(err, ErrConfig), errors.Is(err, connect.ErrEndpoint), errors.Is(err, connect.ErrPort), errors.Is(err, project.ErrRegistry):
		return "config"
	case errors.Is(err, install.ErrBusy):
		return "busy"
	case errors.Is(err, install.ErrHost):
		return "host"
	case errors.Is(err, connect.ErrDenied):
		return "connect_denied"
	case errors.Is(err, connect.ErrUnavailable), errors.Is(err, ErrChanging):
		return "connect_unavailable"
	case errors.Is(err, connect.ErrResponse), errors.Is(err, connect.ErrRedirect),
		errors.Is(err, connect.ErrNotLoopback), errors.Is(err, connect.ErrIdentifier):
		return "connect_response"
	case errors.Is(err, project.ErrContent):
		return "content"
	case errors.Is(err, ErrUnhealthy):
		return "web_unhealthy"
	}
	return "internal"
}

// Result is what a run did.
type Result struct {
	// Outcome is "current" when the vault and the installed files were
	// unchanged, "rendered" after a full read.
	Outcome string
	// Changed reports whether any installed file changed; WebChanged whether
	// the application environment did; Restarted whether the web container was
	// restarted for it.
	Changed, WebChanged, Restarted bool
}

// Runner is one renderer.
type Runner struct {
	Config Config
	Mounts install.MountChecker
	Web    Web
	Log    *slog.Logger
	Now    func() time.Time
	// Sleep waits between attempts; tests replace it.
	Sleep func(context.Context, time.Duration) error
	// PortFloor reads the lowest port any account may listen on
	// (net.ipv4.ip_unprivileged_port_start). The endpoint's port must be below
	// it, or "privileged" proves nothing about who answers.
	PortFloor func() (int, error)
	// open makes the Connect client for a test's in-process server; only
	// tests set it. Unset, the client is connect.New and nothing else.
	open func(endpoint string, token connect.Token) (*connect.Client, error)
	// fault fails the run at a named step; only tests set it.
	fault func(step string) error
}

func (r *Runner) at(step string) error {
	if r.fault == nil {
		return nil
	}
	return r.fault(step)
}

type hostRefusal struct{ message string }

func (e *hostRefusal) Error() string { return e.message }
func (e *hostRefusal) Unwrap() error { return install.ErrHost }

// privileged refuses a host where the endpoint's port is one any account may
// listen on: the port rule rests on the kernel reserving it for root.
func (r *Runner) privileged() error {
	port, err := connect.Port(r.Config.Endpoint)
	if err != nil {
		return err
	}
	if r.PortFloor == nil {
		return &hostRefusal{"The host's unprivileged port floor cannot be read, so the Connect port cannot be shown to be root's."}
	}
	floor, err := r.PortFloor()
	if err != nil {
		return &hostRefusal{"The host's unprivileged port floor cannot be read, so the Connect port cannot be shown to be root's."}
	}
	if port >= floor {
		return &hostRefusal{"Any account can listen on the Connect port on this host: net.ipv4.ip_unprivileged_port_start is " +
			strconv.Itoa(floor) + ", and the port must be below it."}
	}
	return nil
}

var credentialName = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`)

// credential reads one of the unit's credentials. Its bytes never leave this
// process by argument, environment, log line or error.
func (r *Runner) credential(name string) ([]byte, error) {
	if !filepath.IsAbs(r.Config.CredentialsDir) {
		return nil, errConfig("CREDENTIALS_DIRECTORY is required: the token comes from the unit's credentials")
	}
	if !credentialName.MatchString(name) || name == "." || name == ".." {
		return nil, errConfig("a credential name is required and must be a plain name")
	}
	root, err := os.OpenRoot(r.Config.CredentialsDir)
	if err != nil {
		return nil, errConfig("the credentials directory could not be opened")
	}
	defer root.Close()
	file, err := root.Open(name)
	if err != nil {
		return nil, errConfig("a credential could not be read")
	}
	defer file.Close()
	if info, err := file.Stat(); err != nil || !info.Mode().IsRegular() {
		return nil, errConfig("a credential is not a regular file")
	}
	data, err := io.ReadAll(io.LimitReader(file, 64<<10+1))
	if err != nil || len(data) > 64<<10 {
		return nil, errConfig("a credential could not be read")
	}
	return data, nil
}

func (r *Runner) validate() error {
	config := r.Config
	switch {
	case config.Vault == "":
		return errConfig("SEVERINO_SECRETS_VAULT is required")
	case config.EnvItem == "":
		return errConfig("SEVERINO_ENV_ITEM is required")
	case config.ConnectCredential == "":
		return errConfig("SEVERINO_CONNECT_CREDENTIAL is required")
	case r.open == nil && connect.CheckEndpoint(config.Endpoint) != nil:
		return connect.CheckEndpoint(config.Endpoint)
	case config.MinAppVariables < 1 || config.ConnectTimeout <= 0 || config.FullEvery <= 0:
		return errConfig("the renderer's bounds must be positive")
	}
	return nil
}

func sameVersion(a, b *int) bool { return a != nil && b != nil && *a == *b }

// resolve finds the configured vault among those the token reaches, by
// identifier or by name, and refuses anything but exactly one.
func resolve(vaults []connectapi.Vault, configured string) (connectapi.Vault, error) {
	var found []connectapi.Vault
	for _, vault := range vaults {
		if (vault.Id != nil && *vault.Id == configured) || (vault.Name != nil && *vault.Name == configured) {
			found = append(found, vault)
		}
	}
	if len(found) != 1 || found[0].Id == nil || !connect.ValidID(*found[0].Id) {
		return connectapi.Vault{}, &connect.Error{Op: "resolve vault", Class: connect.ErrResponse, Detail: "the vault must resolve uniquely"}
	}
	return found[0], nil
}

// read is one consistent read of the vault: every active item in full,
// bracketed by the vault's content version. A vault edited mid-read is read
// again, so the files installed never mix two states of it.
func (r *Runner) read(ctx context.Context, client *connect.Client, id string) (connectapi.Vault, []connectapi.FullItem, error) {
	for range 3 {
		before, err := client.Vault(ctx, id)
		if err != nil {
			return before, nil, err
		}
		listing, err := client.Items(ctx, id)
		if err != nil {
			return before, nil, err
		}
		items := make([]connectapi.FullItem, 0, len(listing))
		stable := true
		for _, listed := range listing {
			// An archived or deleted item is one the operator retired.
			if listed.State != nil && *listed.State != "" {
				continue
			}
			item, err := client.Item(ctx, id, *listed.Id)
			if err != nil {
				return before, nil, err
			}
			if listed.Version != nil && item.Version != nil && *listed.Version != *item.Version {
				stable = false
				break
			}
			items = append(items, item)
		}
		after, err := client.Vault(ctx, id)
		if err != nil {
			return before, nil, err
		}
		unversioned := before.ContentVersion == nil && after.ContentVersion == nil
		if stable && (unversioned || sameVersion(before.ContentVersion, after.ContentVersion)) {
			return after, items, nil
		}
	}
	return connectapi.Vault{}, nil, ErrChanging
}

func (r *Runner) inputs(salt string, registry []byte) string {
	config := r.Config
	// The document's version is an input: a renderer that writes another
	// format never takes an older render for a current one.
	return digest(salt, []byte("hq-secrets/1"), registry, []byte(config.Vault), []byte(config.EnvItem),
		[]byte(config.Layout.SecretDir), []byte(config.Layout.RuntimeDir), []byte(config.Layout.WebDir),
		[]byte(strconv.Itoa(config.MinAppVariables)), []byte(strconv.Itoa(connections.SchemaVersion)))
}

// Run renders once. It returns after the lock is released and the staging
// directory is gone, whatever happened.
func (r *Runner) Run(ctx context.Context) (Result, error) {
	if err := r.validate(); err != nil {
		return Result{}, err
	}
	if r.open == nil {
		if err := r.privileged(); err != nil {
			return Result{}, err
		}
	}
	// The host first: nothing is read from Connect onto a directory that could
	// reach a disk.
	tree, err := install.Open(r.Config.Layout, r.Mounts)
	if err != nil {
		return Result{}, err
	}
	defer tree.Close()
	unlock, err := tree.Lock()
	if err != nil {
		return Result{}, err
	}
	defer unlock()

	status := secretstatus.Status{SchemaVersion: secretstatus.SchemaVersion}
	if data, ok := install.Read(tree.Runtime(), secretstatus.Name, r.Config.Layout.RootUID, 0o644); ok {
		if previous, err := secretstatus.Decode(data); err == nil {
			status = previous
		}
	}
	result, err := r.render(ctx, tree, &status)
	// Whatever the render did, a container that has not loaded the installed
	// environment is restarted now: after a failure, on an unchanged vault,
	// and again every run until it comes back healthy.
	if settleErr := r.settle(ctx, tree, &result); err == nil {
		err = settleErr
	}
	status.LastAttempt = secretstatus.Attempt{At: r.Now().UTC(), Outcome: result.Outcome, Failure: Class(err)}
	if err != nil {
		status.LastAttempt.Outcome = secretstatus.OutcomeFailed
	}
	if data, marshalErr := json.Marshal(status); marshalErr != nil || tree.WriteAtomic(secretstatus.Name, append(data, '\n'), 0o644) != nil {
		r.Log.Warn("the status document could not be written", slog.String("event", "secrets.status.unwritten"))
	}
	return result, err
}

func (r *Runner) render(ctx context.Context, tree *install.Tree, status *secretstatus.Status) (Result, error) {
	config := r.Config
	if err := tree.LegacyKeys(); err != nil {
		return Result{}, err
	}
	registryBytes, err := os.ReadFile(config.RegistryPath)
	if err != nil {
		return Result{}, errConfig("the connection registry could not be read")
	}
	registry, err := project.ParseRegistry(registryBytes)
	if err != nil {
		return Result{}, err
	}
	raw, err := r.credential(config.ConnectCredential)
	if err != nil {
		return Result{}, err
	}
	token, err := connect.NewToken(raw)
	if err != nil {
		return Result{}, errConfig(err.Error())
	}
	open := connect.New
	if r.open != nil {
		open = r.open
	}
	client, err := open(config.Endpoint, token)
	if err != nil {
		return Result{}, err
	}
	client.Sleep = r.Sleep

	connectCtx, cancel := context.WithTimeout(ctx, config.ConnectTimeout)
	defer cancel()
	// Prove the endpoint and the token before anything else is asked of it.
	vaults, attempts, err := client.WaitReady(connectCtx)
	if health, healthErr := client.Health(connectCtx); healthErr == nil {
		status.Connect = connectStatus(health, r.Now().UTC())
	}
	if err != nil {
		return Result{}, err
	}
	if attempts > 1 {
		r.Log.Info("Connect answered after retries", slog.String("event", "secrets.connect.ready"), slog.Int("attempts", attempts))
	}
	vault, err := resolve(vaults, config.Vault)
	if err != nil {
		return Result{}, err
	}

	// An unchanged vault is not re-read item by item every hour, as long as
	// every installed file is still the one the last full read installed.
	if last, ok := r.loadState(tree); ok && vault.ContentVersion != nil &&
		last.VaultID == *vault.Id && last.ContentVersion == *vault.ContentVersion &&
		((last.AttributeVersion == nil && vault.AttributeVersion == nil) || sameVersion(last.AttributeVersion, vault.AttributeVersion)) &&
		last.Inputs == r.inputs(last.Salt, registryBytes) &&
		r.Now().Sub(last.RenderedAt) < config.FullEvery && r.Now().After(last.RenderedAt.Add(-time.Minute)) &&
		!r.pendingUnreadable(tree) && r.intact(tree, last) {
		status.LastSuccess = &secretstatus.Success{At: r.Now().UTC(), RenderedAt: last.RenderedAt, ContentVersion: vault.ContentVersion,
			AttributeVersion: vault.AttributeVersion, Counts: last.Counts}
		r.Log.Info("Severino HQ secrets are current.", slog.String("event", "secrets.render.current"),
			slog.Int("content_version", *vault.ContentVersion))
		return Result{Outcome: secretstatus.OutcomeCurrent}, nil
	}
	read, items, err := r.read(connectCtx, client, *vault.Id)
	if err != nil {
		return Result{}, err
	}
	out, err := project.Project(project.Input{
		Registry: registry, EnvItem: config.EnvItem, Items: items,
		MinAppVariables: config.MinAppVariables,
		Vault:           project.Vault{Configured: config.Vault, ID: *vault.Id, Name: text(vault.Name)},
	})
	if err != nil {
		return Result{}, err
	}
	encoded, err := out.Document.Encode()
	if err != nil {
		return Result{}, err
	}

	// Retrieval and validation are complete. Stage what is renamed into place;
	// only then is anything installed touched.
	layout := config.Layout
	stage, err := tree.NewStage()
	if err != nil {
		return Result{}, err
	}
	defer stage.Close()
	if err := stage.Write("connections", encoded, layout.RootUID, layout.RootGID, 0o400); err != nil {
		return Result{}, err
	}
	if err := stage.Mkdir(install.SSHDirName); err != nil {
		return Result{}, err
	}
	for _, file := range out.SSH {
		if err := stage.Write(install.SSHDirName+"/"+file.Name, file.Data, layout.RootUID, layout.RootGID, file.Mode); err != nil {
			return Result{}, err
		}
	}

	result := Result{Outcome: secretstatus.OutcomeRendered}
	// Every installed file changes under the lock the launcher copies them
	// with, the application environment included: its in-place write truncates
	// first. Taken before anything is touched, so a launcher that holds it
	// longer than the wait leaves this run with nothing half done.
	unlock, err := tree.LockSSH(ctx)
	if err != nil {
		return result, err
	}
	defer unlock()
	if err := tree.RemoveMCPToken(); err != nil {
		return result, err
	}
	// The restart is owed from before the first byte is written until the
	// container is healthy on it. A run that dies in between leaves the mark,
	// and the next run, finding the file already equal, still restarts.
	_, owed, _ := r.pending(tree)
	checkout := tree.HasCheckoutEnv()
	current, ok := install.Read(tree.Web(), install.AppEnvName, layout.WebUID, 0o400)
	differs := !ok || !bytes.Equal(current, out.AppEnv)
	if checkout && !differs {
		current, ok = install.Read(tree.Secret(), install.AppEnvName, layout.WebUID, 0o400)
		differs = !ok || !bytes.Equal(current, out.AppEnv)
	}
	if differs || owed {
		if err := r.markPending(tree, out.AppEnv); err != nil {
			return result, err
		}
	}
	if err := r.at("marked"); err != nil {
		return result, err
	}
	// Existing bind mounts require in-place updates; this is not a multi-file
	// transaction.
	if checkout {
		changed, err := install.InPlace(tree.Secret(), install.AppEnvName, out.AppEnv, layout.WebUID, layout.WebGID, 0o400)
		if err != nil {
			return result, err
		}
		result.WebChanged = result.WebChanged || changed
	}
	changed, err := install.InPlace(tree.Web(), install.AppEnvName, out.AppEnv, layout.WebUID, layout.WebGID, 0o400)
	if err != nil {
		return result, err
	}
	result.WebChanged = result.WebChanged || changed
	result.Changed = result.WebChanged
	if err := r.at("web-env"); err != nil {
		return result, err
	}

	controllerChanged, err := r.installController(tree, stage, encoded, out)
	result.Changed = result.Changed || controllerChanged
	if err != nil {
		return result, err
	}
	if err := r.at("controller"); err != nil {
		return result, err
	}

	// Read every installed file back: the state records what is there, not
	// what was meant to be.
	salt, err := newSalt()
	if err != nil {
		return result, errors.New("no randomness for the render state")
	}
	next := state{SchemaVersion: stateSchemaVersion, RenderedAt: r.Now().UTC(), VaultID: *vault.Id,
		AttributeVersion: read.AttributeVersion, Salt: salt, Inputs: r.inputs(salt, registryBytes),
		Counts: secretstatus.Counts{ItemsRead: len(items), Connections: len(out.Document.Connections), AppVariables: out.AppVariables,
			Identities: out.Identities, SigningKeys: out.SigningKeys}}
	type target struct {
		dir, name string
		uid       int
		mode      os.FileMode
	}
	targets := []target{{"web", install.AppEnvName, layout.WebUID, 0o400}, {"runtime", install.ConnectionsName, layout.RootUID, 0o400}}
	if checkout {
		targets = append(targets, target{"checkout", install.AppEnvName, layout.WebUID, 0o400})
	}
	for _, file := range out.SSH {
		targets = append(targets, target{"runtime", install.SSHDirName + "/" + file.Name, layout.RootUID, file.Mode})
	}
	for _, each := range targets {
		recorded, ok := r.record(tree, salt, each.dir, each.name, each.uid, each.mode)
		if !ok {
			return result, &installFailure{}
		}
		next.Files = append(next.Files, recorded)
	}
	if read.ContentVersion != nil {
		next.ContentVersion = *read.ContentVersion
		if err := r.saveState(tree, next); err != nil {
			return result, err
		}
	}
	if err := r.at("state"); err != nil {
		return result, err
	}
	status.LastSuccess = &secretstatus.Success{At: next.RenderedAt, RenderedAt: next.RenderedAt, ContentVersion: read.ContentVersion,
		AttributeVersion: read.AttributeVersion, Counts: next.Counts}
	r.Log.Info("Severino HQ secrets rendered.", slog.String("event", "secrets.render.installed"),
		slog.Bool("changed", result.Changed), slog.Bool("web_changed", result.WebChanged),
		slog.Int("items_read", next.Counts.ItemsRead), slog.Int("connections", next.Counts.Connections),
		slog.Int("app_variables", next.Counts.AppVariables), slog.Int("identities", next.Counts.Identities),
		slog.Int("signing_keys", next.Counts.SigningKeys))
	return result, nil
}

type installFailure struct{}

func (*installFailure) Error() string {
	return "An installed file could not be read back as the renderer's own."
}
func (*installFailure) Unwrap() error { return install.ErrHost }

// installController moves the connections document and the identities into
// place as one generation. The caller holds the lock the launcher reads them
// with.
func (r *Runner) installController(tree *install.Tree, stage *install.Stage, encoded []byte, out project.Output) (bool, error) {
	changed, err := stage.Rename("connections", install.ConnectionsName, encoded, 0o400, nil)
	if err != nil {
		return changed, err
	}
	if err := tree.EnsureSSHDir(); err != nil {
		return changed, err
	}
	wanted := map[string]bool{}
	for _, file := range out.SSH {
		name := install.SSHDirName + "/" + file.Name
		wanted[file.Name] = true
		moved, err := stage.Rename(name, name, file.Data, file.Mode, file.Same)
		changed = changed || moved
		if err != nil {
			return changed, err
		}
	}
	pruned, err := tree.PruneSSH(wanted)
	return changed || pruned, err
}

// restart restarts the web container for a changed application environment
// and waits for it to report healthy.
func (r *Runner) restart(ctx context.Context) error {
	if err := r.Web.Restart(ctx); err != nil {
		return ErrUnhealthy
	}
	for range r.Config.HealthAttempts {
		if r.Web.Health(ctx) == "healthy" {
			return nil
		}
		if r.Sleep(ctx, r.Config.HealthInterval) != nil {
			break
		}
	}
	return ErrUnhealthy
}

func text(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
