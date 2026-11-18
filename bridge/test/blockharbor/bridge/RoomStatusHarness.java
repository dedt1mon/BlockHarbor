package blockharbor.bridge;

import link.e4steam.api.ApiResult;
import link.e4steam.api.E4steamApi;
import link.e4steam.api.Registration;
import link.e4steam.api.ResourceScope;
import link.e4steam.api.addon.AddonContext;
import link.e4steam.api.addon.AddonDescriptor;
import link.e4steam.api.identity.IdentityService;
import link.e4steam.api.identity.IdentityService.MinecraftIdentity;
import link.e4steam.api.identity.IdentityService.PeerId;
import link.e4steam.api.identity.IdentityService.PeerIdentity;
import link.e4steam.api.identity.IdentityService.RemoteIdentity;
import link.e4steam.api.session.SessionService;
import link.e4steam.api.session.SessionService.PeerPage;
import link.e4steam.api.session.SessionService.PeerSnapshot;
import link.e4steam.api.session.SessionService.SessionId;
import link.e4steam.api.session.SessionService.SessionRole;
import link.e4steam.api.session.SessionService.SessionSnapshot;
import link.e4steam.api.session.SessionService.SessionState;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicReference;

/** Contract test against the actual upstream API JAR; no Steam or MC claims. */
public final class RoomStatusHarness {
    private RoomStatusHarness() { }

    private static final class Scope implements ResourceScope {
        final List<Registration> children = new ArrayList<Registration>();
        boolean closed;
        @Override public <T extends Registration> T own(T child) { children.add(child); return child; }
        @Override public int openResourceCount() { return closed ? 0 : children.size(); }
        @Override public void close() { for (Registration child : children) child.close(); closed = true; }
        @Override public boolean isClosed() { return closed; }
    }

    private static <T> T proxy(Class<T> type, InvocationHandler handler) {
        return type.cast(Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[] {type}, handler));
    }

    private static String await(Path status, String contains) throws Exception {
        long deadline = System.nanoTime() + 6000000000L;
        while (System.nanoTime() < deadline) {
            if (Files.exists(status)) {
                String data = new String(Files.readAllBytes(status), StandardCharsets.UTF_8);
                if (data.contains(contains)) return data;
            }
            Thread.sleep(40);
        }
        throw new AssertionError("Bridge did not publish expected status: " + contains);
    }

    public static void main(String[] args) throws Exception {
        Path root = Paths.get(args[0]).toAbsolutePath();
        System.setProperty("user.dir", root.toString());
        Path directory = root.resolve(".blockharbor-room");
        Files.createDirectories(directory);
        String nonce = "0123456789abcdef0123456789abcdef0123456789abcdef";
        Files.write(directory.resolve("request.json"), ("{\"schema\":1,\"nonce\":\"" + nonce + "\",\"issuedAt\":" + System.currentTimeMillis() + "}").getBytes(StandardCharsets.UTF_8));
        final AtomicReference<SessionState> phase = new AtomicReference<SessionState>(SessionState.ACTIVE);
        final SessionService sessions = proxy(SessionService.class, new InvocationHandler() {
            @Override public Object invoke(Object proxy, Method method, Object[] arguments) {
                if ("snapshot".equals(method.getName())) return ApiResult.success(new SessionSnapshot(new SessionId("testsession", 1), SessionRole.INTEGRATED_HOST, phase.get(), 2, 8, Collections.emptySet()));
                if ("peers".equals(method.getName())) return CompletableFuture.completedFuture(ApiResult.success(new PeerPage(Arrays.asList(
                        new PeerSnapshot(new PeerId("peer00001"), true), new PeerSnapshot(new PeerId("peer00002"), true)), "")));
                throw new AssertionError("Unexpected session action: " + method.getName());
            }
        });
        final IdentityService identities = proxy(IdentityService.class, new InvocationHandler() {
            @Override public Object invoke(Object proxy, Method method, Object[] arguments) {
                if (!"remote".equals(method.getName())) throw new AssertionError("Unexpected identity action");
                PeerId peer = (PeerId) arguments[0];
                String name = peer.value().equals("peer00001") ? "Alice" : "Игрок 🎮";
                return CompletableFuture.completedFuture(ApiResult.success(new RemoteIdentity(new PeerIdentity(peer,
                        new MinecraftIdentity(UUID.nameUUIDFromBytes(name.getBytes(StandardCharsets.UTF_8)), name, false)))));
            }
        });
        final E4steamApi api = proxy(E4steamApi.class, new InvocationHandler() {
            @Override public Object invoke(Object proxy, Method method, Object[] arguments) {
                if ("sessions".equals(method.getName())) return sessions;
                if ("identities".equals(method.getName())) return identities;
                throw new AssertionError("Unexpected API service: " + method.getName());
            }
        });
        final Scope scope = new Scope();
        final RoomStatusAddon addon = new RoomStatusAddon();
        addon.initialize(new AddonContext() {
            @Override public AddonDescriptor descriptor() { return addon.descriptor(); }
            @Override public E4steamApi api() { return api; }
            @Override public ResourceScope resources() { return scope; }
        });
        Path status = directory.resolve("status.json");
        String first = await(status, "\"state\":\"ACTIVE\"");
        if (!first.contains("\"peers\":2") || !first.contains("Alice") || !first.contains("Игрок 🎮")
                || !first.contains(nonce) || !first.contains("\"heapUsed\":")) throw new AssertionError("Incomplete active snapshot");
        phase.set(SessionState.CLOSED);
        String closed = await(status, "\"state\":\"CLOSED\"");
        if (!closed.contains("\"names\":[]")) throw new AssertionError("Names retained after session close");
        scope.close();
        long modified = Files.getLastModifiedTime(status).toMillis();
        Thread.sleep(2300);
        if (Files.getLastModifiedTime(status).toMillis() != modified) throw new AssertionError("Writer not closed with resource scope");
        if (!RoomStatusAddon.quote("a\"b\\c\n").equals("\"a\\\"b\\\\c\\u000a\"")) throw new AssertionError("JSON quoting failed");
        System.out.println("PASS: API linkage, authenticated names, current session, bounded JSON, scope cleanup");
    }
}
