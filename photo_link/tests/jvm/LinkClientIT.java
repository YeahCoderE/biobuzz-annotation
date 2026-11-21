import com.biobuzz.photolink.core.LinkClient;
import com.biobuzz.photolink.core.Outbox;
import com.biobuzz.photolink.core.Pairing;

import java.io.File;
import java.io.FileOutputStream;
import java.net.DatagramSocket;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import javax.net.SocketFactory;

/** Runs the phone's real connection code (LinkClient) against the PC receiver.
 *  java LinkClientIT <port> <token> <photos> <mbPerPhoto> <hosts,comma> [connections] [timeoutSec] */
public class LinkClientIT {
    public static void main(String[] a) throws Exception {
        int port = Integer.parseInt(a[0]);
        String token = a[1];
        int photos = Integer.parseInt(a[2]);
        double mb = Double.parseDouble(a[3]);
        List<String> hosts = new ArrayList<>(List.of(a[4].split(",")));
        int conns = a.length > 5 ? Integer.parseInt(a[5]) : 2;
        int timeout = a.length > 6 ? Integer.parseInt(a[6]) : 120;

        File base = java.nio.file.Files.createTempDirectory("bbphone").toFile();
        Outbox outbox = new Outbox(base);
        int size = (int) (mb * 1024 * 1024);
        long baseTime = System.currentTimeMillis() - 3_600_000L - (System.nanoTime() % 100_000L) * 1000L;
        String runTag = Long.toHexString(System.nanoTime() & 0xffffffffL);
        for (int i = 0; i < photos; i++) {
            byte[] b = new byte[size];
            new java.util.Random(i).nextBytes(b);
            b[0] = (byte) 0xff; b[1] = (byte) 0xd8; b[2] = (byte) 0xff; b[3] = (byte) 0xe0;
            String id = String.format("it%03d%s", i, runTag);
            File tmp = outbox.tempFile(id, baseTime + i * 1000L);
            try (FileOutputStream o = new FileOutputStream(tmp)) { o.write(b); }
            outbox.commit(tmp);
        }
        AtomicInteger sent = new AtomicInteger(), dup = new AtomicInteger(), rejected = new AtomicInteger();
        Pairing p = new Pairing(hosts, port, token);

        LinkClient.Env env = new LinkClient.Env() {
            public SocketFactory socketFactory() { return null; }
            public void bindDatagram(DatagramSocket s) { }
            public String localIp() { return "192.168.1.10"; }
            public int battery() { return 77; }
            public boolean charging() { return false; }
            public int rssi() { return -55; }
            public String deviceId() { return "jvmtestdevice001"; }
            public String deviceName() { return "JVM test phone"; }
        };
        LinkClient.Listener l = new LinkClient.Listener() {
            public void onChange() { }
            public void onProgress(String id, long s, long t) { }
            public void onSent(File f, String id, String name, boolean d, long bytes) { sent.incrementAndGet(); if (d) dup.incrementAndGet(); }
            public void onRejected(File f, String id, String code) { rejected.incrementAndGet(); }
            public void onFatal(String code) { System.out.println("FATAL " + code); }
            public void onPairingUpdated(Pairing pp) { }
            public void onLog(String line) { System.out.println("  [client] " + line); }
        };
        LinkClient c = new LinkClient(outbox, env, l);
        c.setConnections(conns);
        long t0 = System.nanoTime();
        c.start(p);
        double maxMbps = 0;
        while (outbox.count() > 0 && (System.nanoTime() - t0) / 1e9 < timeout) {
            Thread.sleep(250);
            maxMbps = Math.max(maxMbps, c.mbpsNow());
            if (c.state() == LinkClient.State.FATAL) break;
        }
        double sec = (System.nanoTime() - t0) / 1e9;
        Thread.sleep(600);
        System.out.printf("RESULT sent=%d dup=%d rejected=%d left=%d state=%s sockets=%d time=%.1fs rate=%.0f Mbps (peak ticker %.0f) latency=%dms%n",
                sent.get(), dup.get(), rejected.get(), outbox.count(), c.state(), c.sockets(), sec,
                photos * size * 8.0 / 1e6 / sec, maxMbps, c.latencyMs());
        if (a.length > 7 && a[7].equals("speed")) System.out.printf("SPEEDTEST %.0f Mbps%n", c.speedTest(8L << 20));
        c.stop();
        System.exit(outbox.count() == 0 ? 0 : 2);
    }
}
