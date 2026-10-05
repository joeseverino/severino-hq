package secrets

import (
	"time"

	"github.com/joeseverino/severino-hq/controller/secrets/connect"
	"github.com/joeseverino/severino-hq/controller/secretstatus"
)

// connectStatus keeps only short words of what the server said: the document
// is read by HQ, and what an unauthenticated endpoint returned is not trusted
// to be harmless text.
func connectStatus(health connect.Health, at time.Time) *secretstatus.Connect {
	found := &secretstatus.Connect{ReadAt: at, Version: secretstatus.Word(&health.Version), Dependencies: []secretstatus.Dependency{}}
	for index, dependency := range health.Dependencies {
		if index == secretstatus.MaxDependencies {
			break
		}
		found.Dependencies = append(found.Dependencies, secretstatus.Dependency{
			Service: secretstatus.Word(dependency.Service), Status: secretstatus.Word(dependency.Status)})
	}
	return found
}
