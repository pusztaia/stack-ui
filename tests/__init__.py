import socketserver

# serve_forever() looks for a stop request every 0.5 s by default, and shutdown() waits for that look: a test with the mock
# libpod and a server of the page lost up to a second just stopping them (about 0.9 s of each test's 1.2 s). A short poll
# makes the whole suite several times faster. Only the tests import this package, never the page itself.
assert socketserver.BaseServer.serve_forever.__defaults__ == (0.5,), 'serve_forever changed: look at this again'
socketserver.BaseServer.serve_forever.__defaults__ = (0.01,)
