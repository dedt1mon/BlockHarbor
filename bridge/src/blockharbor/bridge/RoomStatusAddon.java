package blockharbor.bridge;

import link.e4steam.api.ApiResult;
import link.e4steam.api.ApiVersion;
import link.e4steam.api.ApiVersionRange;
import link.e4steam.api.E4steamApi;
import link.e4steam.api.Registration;
import link.e4steam.api.addon.AddonContext;
import link.e4steam.api.addon.AddonDescriptor;
import link.e4steam.api.addon.AddonId;
import link.e4steam.api.addon.E4steamAddonEntrypoint;
import link.e4steam.api.capability.Capabilities;
import link.e4steam.api.capability.CapabilityId;
import link.e4steam.api.identity.IdentityService.RemoteIdentity;
import link.e4steam.api.session.SessionService.PeerPage;
import link.e4steam.api.session.SessionService.PeerSnapshot;
import link.e4steam.api.session.SessionService.SessionSnapshot;

import java.lang.management.ManagementFactory;
import java.lang.management.MemoryUsage;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Read-only bridge. No network listener, launcher credential or game control. */
public final class RoomStatusAddon implements E4steamAddonEntrypoint {
    private static final Pattern NONCE = Pattern.compile("\\\"nonce\\\"\\s*:\\s*\\\"([a-f0-9]{48})\\\"");
    private static final Pattern ISSUED = Pattern.compile("\\\"issuedAt\\\"\\s*:\\s*([0-9]{10,16})");

    @Override
    public AddonDescriptor descriptor() {
        Set<CapabilityId> capabilities = new LinkedHashSet<CapabilityId>(Arrays.asList(
                Capabilities.SESSION_OBSERVE, Capabilities.IDENTITY_MINECRAFT_READ));
        return new AddonDescriptor(new AddonId("blockharbor:room_status"), "BlockHarbor Room Status",
                ApiVersion.parse("1.0.0"),
                new ApiVersionRange(ApiVersion.parse("1.1.0"), ApiVersion.parse("2.0.0")),
                Collections.emptyList(), capabilities, capabilities);
    }

    @Override
    public void initialize(AddonContext context) throws Exception {
        Path root = Paths.get(System.getProperty("user.dir")).toAbsolutePath().normalize().toRealPath();
        Path directory = root.resolve(".blockharbor-room");
        Path request = directory.resolve("request.json");
        if (!Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS)
                || !directory.toRealPath().equals(directory)
                || !Files.isRegularFile(request, LinkOption.NOFOLLOW_LINKS)
                || Files.size(request) > 1024) return;
        String data = new String(Files.readAllBytes(request), StandardCharsets.UTF_8);
        Matcher match = NONCE.matcher(data);
        if (!match.find()) return;
        String nonce = match.group(1);
        if (match.find()) return;
        Matcher issued = ISSUED.matcher(data);
        if (!issued.find()) return;
        long age = System.currentTimeMillis() - Long.parseLong(issued.group(1));
        if (issued.find() || age < -10000 || age > 21600000) return;
        Bridge bridge = new Bridge(context.api(), directory, nonce);
        context.resources().own(bridge);
        bridge.start();
    }

    private static final class Bridge implements Registration {
        private final E4steamApi api;
        private final Path directory;
        private final String nonce;
        private final AtomicBoolean closed = new AtomicBoolean();
        private final ScheduledExecutorService worker;
        private long sequence;

        Bridge(E4steamApi api, Path directory, String nonce) {
            this.api = api;
            this.directory = directory;
            this.nonce = nonce;
            worker = Executors.newSingleThreadScheduledExecutor(new ThreadFactory() {
                @Override public Thread newThread(Runnable runnable) {
                    Thread thread = new Thread(runnable, "BlockHarbor room status");
                    thread.setDaemon(true);
                    return thread;
                }
            });
        }

        void start() {
            worker.scheduleWithFixedDelay(new Runnable() {
                @Override public void run() {
                    if (closed.get()) return;
                    try { sample(); }
                    catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
                    catch (Exception unavailable) {
                        // Never dump exceptions, Steam details or identities to a log.
                        // A failed sample becomes stale in the launcher after 8 seconds.
                    }
                }
            }, 0, 2, TimeUnit.SECONDS);
        }

        private void sample() throws Exception {
            ApiResult<SessionSnapshot> result = api.sessions().snapshot();
            SessionSnapshot session = result.value().orElse(null);
            String state = session == null ? "UNAVAILABLE" : session.state().name();
            String role = session == null ? "NONE" : session.role().name();
            int peers = session == null ? 0 : session.peers();
            int capacity = session == null ? 0 : session.capacity();
            List<String> names = new ArrayList<String>();
            boolean complete = peers == 0;
            if (session != null && peers > 0 && "ACTIVE".equals(state)) {
                ApiResult<PeerPage> page = api.sessions().peers(session.id(), "", 64)
                        .toCompletableFuture().get(1200, TimeUnit.MILLISECONDS);
                if (page.isSuccess()) {
                    List<CompletableFuture<ApiResult<RemoteIdentity>>> identities = new ArrayList<CompletableFuture<ApiResult<RemoteIdentity>>>();
                    for (PeerSnapshot peer : page.value().get().peers()) {
                        if (peer.authenticated()) identities.add(api.identities().remote(peer.peerId()).toCompletableFuture());
                    }
                    CompletableFuture<?>[] pending = identities.toArray(new CompletableFuture<?>[identities.size()]);
                    try { CompletableFuture.allOf(pending).get(1200, TimeUnit.MILLISECONDS); }
                    catch (java.util.concurrent.TimeoutException incomplete) { /* Keep completed identities only. */ }
                    for (CompletableFuture<ApiResult<RemoteIdentity>> future : identities) {
                        if (!future.isDone() || future.isCompletedExceptionally() || future.isCancelled()) continue;
                        ApiResult<RemoteIdentity> identity = future.getNow(null);
                        if (identity != null && identity.isSuccess()) {
                            String name = identity.value().get().peer().minecraft().displayName();
                            if (name.length() <= 80) names.add(name);
                        }
                    }
                    complete = page.value().get().nextCursor().isEmpty() && names.size() == peers;
                }
            }
            // An identity request may finish after the player closes/reopens a
            // world. Do not publish names belonging to a previous generation.
            SessionSnapshot latest = api.sessions().snapshot().value().orElse(null);
            if (latest == null || session == null || !latest.id().equals(session.id())
                    || latest.state() != session.state() || latest.peers() != peers) {
                names.clear();
                state = latest == null ? "UNAVAILABLE" : latest.state().name();
                role = latest == null ? "NONE" : latest.role().name();
                peers = latest == null ? 0 : latest.peers();
                capacity = latest == null ? 0 : latest.capacity();
                complete = peers == 0;
            }
            MemoryUsage memory = ManagementFactory.getMemoryMXBean().getHeapMemoryUsage();
            StringBuilder out = new StringBuilder(1024);
            out.append("{\"schema\":1,\"nonce\":").append(quote(nonce))
                    .append(",\"sequence\":").append(++sequence)
                    .append(",\"sampledAt\":").append(System.currentTimeMillis())
                    .append(",\"state\":").append(quote(state))
                    .append(",\"role\":").append(quote(role))
                    .append(",\"peers\":").append(peers)
                    .append(",\"capacity\":").append(capacity)
                    .append(",\"namesComplete\":").append(complete)
                    .append(",\"heapUsed\":").append(Math.max(0L, memory.getUsed()))
                    .append(",\"heapMax\":").append(Math.max(0L, memory.getMax()))
                    .append(",\"names\":[");
            for (int i = 0; i < names.size(); i++) {
                if (i > 0) out.append(',');
                out.append(quote(names.get(i)));
            }
            out.append("]}");
            byte[] data = out.toString().getBytes(StandardCharsets.UTF_8);
            if (closed.get() || data.length > 32768 || !directory.toRealPath().equals(directory)) return;
            Path target = directory.resolve("status.json");
            if (Files.isSymbolicLink(target)) return;
            Path temporary = Files.createTempFile(directory, "state-", ".json");
            try {
                Files.write(temporary, data);
                try { Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING); }
                catch (AtomicMoveNotSupportedException unsupported) { Files.move(temporary, target, StandardCopyOption.REPLACE_EXISTING); }
            } finally { Files.deleteIfExists(temporary); }
        }

        @Override public void close() {
            if (closed.compareAndSet(false, true)) worker.shutdownNow();
        }
        @Override public boolean isClosed() { return closed.get(); }
    }

    static String quote(String value) {
        StringBuilder out = new StringBuilder("\"");
        for (int i = 0; i < value.length(); i++) {
            char character = value.charAt(i);
            if (character == '\"' || character == '\\') out.append('\\').append(character);
            else if (character < 32 || character == '\u2028' || character == '\u2029') out.append(String.format(java.util.Locale.ROOT, "\\u%04x", (int) character));
            else out.append(character);
        }
        return out.append('\"').toString();
    }
}
