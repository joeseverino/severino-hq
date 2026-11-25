package providers

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/joeseverino/severino-hq/controller/runtime"
)

// githubContent is GitHub's answer for a file's contents.
func githubContent(text string) string {
	encoded, _ := json.Marshal(map[string]string{"type": "file", "encoding": "base64", "content": base64.StdEncoding.EncodeToString([]byte(text))})
	return string(encoded)
}

// A repository opted in to updates names each app's Compose files on its
// default branch and its open Dependency Dashboard, and each update's pull
// request the image every service moves to, as Compose loads the app's files:
// the override after the main file, a variable's default, a profile's
// service, and a variable no image needs left unread.
func TestAnOptedInRepositoryReadsItsApps(t *testing.T) {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	routes := githubRepositoryRoutes()
	routes[api("/git/trees/main")] = `{"sha":"t0","truncated":false,"tree":[{"path":"compose","mode":"040000","type":"tree","sha":"t1"},{"path":"README.md","mode":"100644","type":"blob","sha":"b0"}]}`
	routes[api("/git/trees/t1?recursive=1")] = `{"sha":"t1","truncated":false,"tree":[
		{"path":"example","mode":"040000","type":"tree","sha":"t2"},
		{"path":"example/compose.yml","mode":"100644","type":"blob","sha":"b1"},
		{"path":"example/docker-compose.override.yml","mode":"100644","type":"blob","sha":"b2"},
		{"path":"example/nginx.conf","mode":"100644","type":"blob","sha":"b3"},
		{"path":"web/compose.yaml","mode":"100644","type":"blob","sha":"b4"},
		{"path":"web/deep/compose.yml","mode":"100644","type":"blob","sha":"b5"}]}`
	routes[api("/issues?state=open&sort=updated&per_page=100")] = `[
		{"title":"Dependency Dashboard","html_url":"https://github.com/example/app/pull/9","user":{"login":"hq-example[bot]"},"pull_request":{"url":"https://api.github.com/repos/example/app/pulls/9"}},
		{"title":"Dependency Dashboard","html_url":"https://github.com/example/app/issues/2","user":{"login":"octo"},"body":" - [ ] <!-- approve-branch=renovate/planted -->Update example/app Docker tag to v9\n"},
		{"title":"Dependency Dashboard","html_url":"https://github.com/example/app/issues/1","user":{"login":"hq-example[bot]"},"body":"## Pending Status Checks\n\n - [ ] <!-- unpend-branch=renovate/example-app-2.x -->Update example/app Docker tag to v2\n - [ ] <!-- approve-branch=renovate/example-worker-5.x -->Update dependency (` + "`example/worker`, `example/cache`" + `)\n - [ ] <!-- rebase-branch=renovate/open -->[Update example/app](../pull/8)\n - [ ] <!-- unschedule-branch=renovate/other -->Update example/app-extra Docker tag to v9\n"}]`
	head := "/contents/compose/%s?ref=aaaa000000000000000000000000000000000000"
	routes[api(fmt.Sprintf(head, "example/compose.yml"))] = githubContent("services:\n  web:\n    image: example/app:1\n  worker:\n    image: example/worker:${TAG:-3}@sha256:" + strings.Repeat("a", 64) + "\n  viewer:\n    build: ./viewer\n    image: example-viewer:local\n  reader:\n    build:\n      context: ./reader\n")
	routes[api(fmt.Sprintf(head, "example/docker-compose.override.yml"))] = githubContent("services:\n  worker:\n    environment:\n      LEVEL: info\n")
	commit := func(sha string) string {
		return `{"sha":"` + sha + `","html_url":"https://github.com/example/app/commit/` + sha + `","commit":{"message":"Change"},"author":null}`
	}
	routes[api("/commits?sha=aaaa000000000000000000000000000000000000&per_page=100")] = "[" + commit("aaaa000000000000000000000000000000000000") + "," + commit("c2") + "," + commit("c1") + "," + commit("c0") + "]"
	routes[api("/commits?sha=aaaa000000000000000000000000000000000000&path=compose%2Fexample&per_page=1")] = "[" + commit("c1") + "]"
	routes[api(fmt.Sprintf(head, "web/compose.yaml"))] = githubContent("services:\n  web:\n    image: example/web:${WEB_TAG}\n")
	routes[api("/contents/compose/example/compose.yml?ref=dddd")] = githubContent("services:\n  web:\n    image: example/app:1\n  worker:\n    image: example/worker:${TAG:-3}\n")
	routes[api("/contents/compose/example/compose.yml?ref=eeee")] = githubContent("services:\n  web:\n    image: example/app:2\n    environment:\n      SECRET: ${SECRET}\n  worker:\n    image: example/worker:${TAG:-3}\n  cache:\n    image: example/cache:7\n    profiles: [cache]\n")
	routes[api("/contents/compose/example/docker-compose.override.yml?ref=dddd")] = githubContent("services:\n  worker:\n    environment:\n      LEVEL: info\n")
	routes[api("/contents/compose/example/docker-compose.override.yml?ref=eeee")] = githubContent("services:\n  worker:\n    image: example/worker:4\n")
	h := newGitHubHarness(t, routes, runtime.Environment{})
	for _, ref := range []string{"dddd", "eeee"} {
		h.refuse(githubFakeAPI, "/repos/example/app/contents/compose/large/compose.yml?ref="+ref, http.StatusNotFound)
	}
	controller := NewController(h.r, runtime.ControllerRegistry{DependencyUpdates: runtime.DependencyUpdatesPlan{
		Owners: []runtime.DependencyUpdatesOwner{{Owner: "example", Repositories: []string{"example/app"}}},
	}})
	report := controller.readKind(t.Context(), h.r.readers[string(runtime.ResourceKindGitHubRepository)])
	if !report.OK || len(report.Records) != 1 || len(report.RefusedParts) != 0 {
		t.Fatalf("report %+v", report)
	}
	record := report.Records[0].(GitHubRepositoryRecord)
	encoded, _ := json.Marshal(record)
	var view map[string]any
	_ = json.Unmarshal(encoded, &view)
	for _, test := range []struct {
		name string
		got  any
		want string
	}{
		{"each app's Compose files, by the names Compose finds in a directory", record.Compose, `["compose/example/compose.yml","compose/example/docker-compose.override.yml","compose/web/compose.yaml"]`},
		{"the open Dashboard issue the update run opened, never a pull request or another's issue", record.Dashboard, `"https://github.com/example/app/issues/1"`},
		{"the login the update run opens them as, the App's bot", view["updater"], `"hq-example[bot]"`},
		{"each service's image at the head, and whether it builds it, an app that needs the host's variables left out", record.ComposeImages, `[{"path":"compose/example/compose.yml","service":"reader","image":"","build":true},{"path":"compose/example/compose.yml","service":"viewer","image":"example-viewer:local","build":true},{"path":"compose/example/compose.yml","service":"web","image":"example/app:1","build":false},{"path":"compose/example/compose.yml","service":"worker","image":"example/worker:3@sha256:` + strings.Repeat("a", 64) + `","build":false}]`},
		{"each app built on its host, and the commits from the last that changed its directory to the head", record.Builds, `[{"app":"example","commits":["aaaa000000000000000000000000000000000000","c2","c1"]}]`},
		{"each branch held with no pull request, and the apps whose image its line names whole", record.HeldBranches, `[{"branch":"renovate/example-app-2.x","marker":"unpend","apps":["example"]},{"branch":"renovate/example-worker-5.x","marker":"approve","apps":["example"]},{"branch":"renovate/other","marker":"unschedule","apps":[]}]`},
		{"each service the update's head moves", record.PullRequests[0].Apps, `[{"app":"example","read":true,"images":[{"service":"cache","before":"","after":"example/cache:7"},{"service":"web","before":"example/app:1","after":"example/app:2"},{"service":"worker","before":"example/worker:3","after":"example/worker:4"}]},{"app":"large","read":true,"images":[]}]`},
		{"no images for a pull request that is not an update", record.PullRequests[1].Apps, `null`},
	} {
		t.Run(test.name, func(t *testing.T) {
			if encoded, _ := json.Marshal(test.got); string(encoded) != test.want {
				t.Fatalf("got  %s\nwant %s", encoded, test.want)
			}
		})
	}
}

// A file an update adds is absent at its base, and an app whose files do not
// load, or whose image needs a variable only the host sets, is read false,
// with no images: the reading never guesses a move. An app whose build inputs
// alone change, at any depth of its directory, is a change that moves no
// image, and a file beside the apps is none.
func TestAnAppThatDoesNotLoadMovesNothing(t *testing.T) {
	api := func(path string) string { return githubFakeAPI + " /repos/example/app" + path }
	routes := githubRepositoryRoutes()
	routes[api("/git/trees/main")] = `{"sha":"t0","truncated":false,"tree":[]}`
	routes[api("/issues?state=open&sort=updated&per_page=100")] = `[]`
	routes[api("/contents/compose/example/compose.yml?ref=eeee")] = githubContent("services:\n  web:\n    image: example/app:2\n")
	routes[api("/contents/compose/large/compose.yml?ref=eeee")] = githubContent("services: [not, a, mapping]\n")
	routes[api("/contents/compose/large/compose.yml?ref=dddd")] = githubContent("services: {}\n")
	routes[api("/pulls/6/files?per_page=100")] = `[{"filename":"compose/example/compose.yml","status":"added","additions":3,"deletions":0},
		{"filename":"compose/large/compose.yml","status":"modified","additions":1,"deletions":1},
		{"filename":"compose/unset/compose.yml","status":"modified","additions":1,"deletions":1},
		{"filename":"compose/built/Dockerfile","status":"modified","additions":1,"deletions":1},
		{"filename":"compose/built/reader/go.mod","status":"modified","additions":1,"deletions":1},
		{"filename":"compose/notes.md","status":"modified","additions":1,"deletions":1}]`
	routes[api("/contents/compose/unset/compose.yml?ref=dddd")] = githubContent("services:\n  db:\n    image: example/db:1\n")
	routes[api("/contents/compose/unset/compose.yml?ref=eeee")] = githubContent("services:\n  db:\n    image: example/db:${DB_TAG}\n")
	h := newGitHubHarness(t, routes, runtime.Environment{})
	h.refuse(githubFakeAPI, "/repos/example/app/contents/compose/example/compose.yml?ref=dddd", http.StatusNotFound)
	controller := NewController(h.r, runtime.ControllerRegistry{DependencyUpdates: runtime.DependencyUpdatesPlan{
		Owners: []runtime.DependencyUpdatesOwner{{Owner: "example", Repositories: []string{"Example/App"}}},
	}})
	report := controller.readKind(t.Context(), h.r.readers[string(runtime.ResourceKindGitHubRepository)])
	if !report.OK || len(report.Records) != 1 {
		t.Fatalf("report %+v", report)
	}
	record := report.Records[0].(GitHubRepositoryRecord)
	want := `[{"app":"built","read":true,"images":[]},{"app":"example","read":true,"images":[{"service":"web","before":"","after":"example/app:2"}]},{"app":"large","read":false,"images":[]},{"app":"unset","read":false,"images":[]}]`
	if encoded, _ := json.Marshal(record.PullRequests[0].Apps); string(encoded) != want || record.Compose == nil || len(record.Compose) != 0 || record.Dashboard != "" {
		t.Fatalf("apps %s, compose %v, dashboard %q", encoded, record.Compose, record.Dashboard)
	}
}
