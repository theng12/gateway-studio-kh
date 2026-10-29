// The console's navigation rule, driven directly: no WebKit, no window, no gateway.
//
// `menubar/ConsoleNavigation.swift` decides whether the console's web view may load a URL in its
// main frame. The Gateway key is injected into that view, so this is the boundary that keeps the
// key from reaching another origin.

import Foundation

private func check(_ condition: Bool, _ what: String) {
    if !condition {
        print("FAIL: \(what)")
        exit(1)
    }
}

@main
struct ConsoleNavigationCheck {
    static func main() {
        let console = URL(string: "http://127.0.0.1:8787/admin")!

        // The console itself, at any view or hash.
        check(ConsoleNavigation.allowsMainFrame(URL(string: "http://127.0.0.1:8787/admin")!, console: console),
              "the console page")
        check(ConsoleNavigation.allowsMainFrame(URL(string: "http://127.0.0.1:8787/admin#fleet:machines")!, console: console),
              "a console view hash")
        check(ConsoleNavigation.allowsMainFrame(URL(string: "http://127.0.0.1:8787/admin/api/fleet")!, console: console),
              "a console API read")

        // The starting page the app draws itself.
        check(ConsoleNavigation.allowsMainFrame(nil, console: console), "no URL at all")
        check(ConsoleNavigation.allowsMainFrame(URL(string: "about:blank"), console: console), "about:blank")
        check(ConsoleNavigation.allowsMainFrame(URL(string: "data:text/html,hi"), console: console), "a data document")

        // Every other origin is refused, including a lookalike host, another port, and a bare host.
        check(!ConsoleNavigation.allowsMainFrame(URL(string: "https://example.com/x"), console: console),
              "another site")
        check(!ConsoleNavigation.allowsMainFrame(URL(string: "http://127.0.0.1:8788/admin"), console: console),
              "another port on this Mac")
        check(!ConsoleNavigation.allowsMainFrame(URL(string: "http://127.0.0.1/admin"), console: console),
              "the default port is a different origin")
        check(!ConsoleNavigation.allowsMainFrame(URL(string: "http://evil.test:8787/admin"), console: console),
              "another host at the same port")
        check(!ConsoleNavigation.allowsMainFrame(URL(string: "http://127.0.0.1.evil.test:8787/admin"), console: console),
              "a host that merely starts with the console's")
        check(!ConsoleNavigation.allowsMainFrame(URL(string: "file:///etc/passwd"), console: console),
              "a local file")
        check(!ConsoleNavigation.allowsMainFrame(URL(string: "https://example.com/x"), console: nil),
              "a foreign link when the console is unknown")

        // A refused web link opens in the browser; nothing else opens anything.
        check(ConsoleNavigation.opensInBrowser(URL(string: "https://example.com")!), "https opens")
        check(ConsoleNavigation.opensInBrowser(URL(string: "http://example.com")!), "http opens")
        check(!ConsoleNavigation.opensInBrowser(URL(string: "file:///etc/passwd")!), "file does not open")
        check(!ConsoleNavigation.opensInBrowser(URL(string: "mailto:a@b.test")!), "mailto does not open")
        check(!ConsoleNavigation.opensInBrowser(nil), "no URL opens nothing")

        print("PASS: console navigation")
    }
}
