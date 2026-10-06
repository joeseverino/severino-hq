package providers

import (
	"errors"
	"testing"

	"github.com/joeseverino/severino-hq/controller/connections"
	"github.com/joeseverino/severino-hq/controller/runtime"
)

// supplied is the connections a test's registry is given.
func supplied(list ...connections.Connection) runtime.Connections {
	return runtime.NewConnections(list...)
}

// loginConnection is a connection that signs in with a username and password.
func loginConnection(provider runtime.ConnectionProvider, ref, url, username, password string) connections.Connection {
	return connections.Connection{Ref: ref, Provider: string(provider), Login: &connections.Login{URL: url, Username: username, Password: password}}
}

// apiTokenConnection is a connection that sends a token to the API at url.
func apiTokenConnection(provider runtime.ConnectionProvider, ref, url, token string) connections.Connection {
	return connections.Connection{Ref: ref, Provider: string(provider), APIToken: &connections.APIToken{URL: url, APIToken: token}}
}

// oauthConnection is a connection that exchanges a client's id and secret.
func oauthConnection(provider runtime.ConnectionProvider, ref, clientID, clientSecret string) connections.Connection {
	return connections.Connection{Ref: ref, Provider: string(provider), OAuthClient: &connections.OAuthClient{ClientID: clientID, ClientSecret: clientSecret}}
}

// githubAppConnection is a GitHub App's connection.
func githubAppConnection(ref, appID, signingKey string) connections.Connection {
	return connections.Connection{Ref: ref, Provider: string(runtime.ConnectionProviderGitHubApp), GitHubApp: &connections.GitHubApp{AppID: appID, SigningKey: signingKey}}
}

// serviceAccountConnection is a 1Password service account's connection.
func serviceAccountConnection(ref, token string) connections.Connection {
	return connections.Connection{Ref: ref, Provider: string(runtime.ConnectionProviderOnePassword), ServiceAccount: &connections.ServiceAccount{APIToken: token}}
}

// sshConnection is an SSH transport that names no provider of its own.
func sshConnection(ref string, transport connections.SSHTransport) connections.Connection {
	return connections.Connection{Ref: ref, Provider: string(runtime.ConnectionProviderSSH), SSHTransport: &transport}
}

// acmeConnection is the ACME account certificates are ordered with.
func acmeConnection(ref, email, directoryURL string) connections.Connection {
	return connections.Connection{Ref: ref, Provider: "acme", ACME: &connections.ACME{Email: email, DirectoryURL: directoryURL}}
}

// managing is the connection with HQ allowed to change things through it.
func managing(connection connections.Connection) connections.Connection {
	connection.Manages = true
	return connection
}

func TestAServiceAccountsTokenIsItsOwn(t *testing.T) {
	r := New(runtime.Environment{}, supplied(
		serviceAccountConnection("publisher", "synthetic"),
		apiTokenConnection(runtime.ConnectionProviderPortainer, "containers", "https://example.invalid", "other"),
	), &fakeHTTP{})
	if token, err := r.onePasswordToken("publisher"); err != nil || token != "synthetic" {
		t.Fatalf("the publisher's token: %v", err)
	}
	if token, err := r.onePasswordToken(""); err != nil || token != "synthetic" {
		t.Fatalf("the only service account: %v", err)
	}
	if _, err := r.onePasswordToken("containers"); !errors.Is(err, runtime.ErrForeignConnection) {
		t.Fatalf("another provider's token was taken: %v", err)
	}
	// A connection of the provider that arrived in another shape has no token to give.
	misfiled := apiTokenConnection(runtime.ConnectionProviderOnePassword, "publisher", "https://example.invalid", "other")
	if _, err := New(runtime.Environment{}, supplied(misfiled), &fakeHTTP{}).onePasswordToken("publisher"); !errors.Is(err, runtime.ErrSettingMissing) {
		t.Fatalf("a token of another shape was read: %v", err)
	}
}
