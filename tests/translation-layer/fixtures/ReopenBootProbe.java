import android.os.Handler;
import android.os.Looper;
import android.view.Choreographer;

/** Deliberately on the boot path, like Husk's framework: app-class replacement cannot reset this state. */
public final class ReopenBootProbe {
    private static final Object marker = new Object();
    private static int frames;
    public static Object marker() { return marker; }
    public static int run(String database) {
        if (frames != 0 || System.getProperty("husk.session.probe") != null) return -2;
        System.setProperty("husk.session.probe", "previous session");
        long db = husk.Sqlite.open(database, 0x10000000);
        husk.Sqlite.busyTimeout(db, 0);
        if (db == 0 || husk.Sqlite.exec(db, "CREATE TABLE IF NOT EXISTS probe(value INTEGER)") != 0
                || husk.Sqlite.exec(db, "BEGIN EXCLUSIVE") != 0
                || husk.Sqlite.exec(db, "INSERT INTO probe VALUES (1)") != 0) return -3;
        long statement = husk.Sqlite.prepare(db, "SELECT COUNT(*) FROM probe");
        if (statement == 0 || husk.Sqlite.step(statement) != husk.Sqlite.ROW || husk.Sqlite.columnLong(statement, 0) != 1) return -4;
        // Deliberately abandon the active statement, exclusive transaction and connection.
        // A fresh session must be able to acquire the database and see the rollback.
        Looper.prepareMainLooper();
        Handler.getMain().postAtTime(() -> Choreographer.getMainThreadInstance().postFrameCallback(time -> {
            frames++;
            Looper.myLooper().quit();
        }), 0);
        Looper.loop();
        return frames;
    }
}
