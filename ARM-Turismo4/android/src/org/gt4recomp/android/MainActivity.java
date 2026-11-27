package org.gt4recomp.android;

import android.app.Activity;
import android.app.NativeActivity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

public class MainActivity extends Activity {
    private static final int REQ_PICK_ISO = 1001;
    private static final String PREFS_NAME = "gt4recomp_prefs";
    private static final String KEY_ISO_PATH = "iso_path";

    private Button pickIsoBtn;
    private Button startBtn;
    private TextView isoStatus;
    private String selectedIsoPath = "";
    private String bundledElfPath = "";
    private boolean isCopying = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        extractBundledElf();

        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        selectedIsoPath = prefs.getString(KEY_ISO_PATH, "");

        if (!selectedIsoPath.isEmpty() && new File(selectedIsoPath).exists()) {
            writeConfigAndLaunch();
            return;
        }

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(48, 64, 48, 64);
        scroll.addView(root);

        TextView title = new TextView(this);
        title.setText("GT4Recomp Setup");
        title.setTextSize(24f);
        title.setPadding(0, 0, 0, 24);
        root.addView(title);

        TextView help = new TextView(this);
        help.setText("core.elf is bundled in the APK. Select your PAL GT4 ISO below (takes ~20-40s to import).");
        help.setTextSize(15f);
        help.setPadding(0, 0, 0, 32);
        root.addView(help);

        pickIsoBtn = new Button(this);
        pickIsoBtn.setText("Select PAL GT4 ISO");
        pickIsoBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (!isCopying) {
                    Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("*/*");
                    startActivityForResult(intent, REQ_PICK_ISO);
                }
            }
        });
        root.addView(pickIsoBtn);

        isoStatus = new TextView(this);
        isoStatus.setText("ISO: Not selected");
        isoStatus.setPadding(0, 12, 0, 48);
        root.addView(isoStatus);

        startBtn = new Button(this);
        startBtn.setText("Save & Start GT4Recomp");
        startBtn.setGravity(Gravity.CENTER);
        startBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (isCopying) return;
                if (selectedIsoPath.isEmpty() || !new File(selectedIsoPath).exists()) {
                    isoStatus.setText("ISO: Please select your PAL GT4 ISO first.");
                    return;
                }
                writeConfigAndLaunch();
            }
        });
        root.addView(startBtn);

        setContentView(scroll);
    }

    private void extractBundledElf() {
        File filesDir = getExternalFilesDir(null);
        if (filesDir == null) return;
        File outElf = new File(filesDir, "core.elf");
        try (InputStream in = getAssets().open("core.elf");
             OutputStream out = new FileOutputStream(outElf)) {
            byte[] buf = new byte[65536];
            int len;
            while ((len = in.read(buf)) > 0) {
                out.write(buf, 0, len);
            }
            out.flush();
            bundledElfPath = outElf.getAbsolutePath();
        } catch (Exception ignored) {
            if (outElf.exists()) {
                bundledElfPath = outElf.getAbsolutePath();
            }
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK_ISO || resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        final Uri uri = data.getData();
        final File target = new File(getExternalFilesDir(null), "GT4Europe.iso");

        isCopying = true;
        pickIsoBtn.setEnabled(false);
        startBtn.setEnabled(false);
        isoStatus.setText("ISO: Importing... 0 MB");

        new Thread(new Runnable() {
            @Override
            public void run() {
                String error = null;
                long totalBytes = 0L;
                try {
                    File parent = target.getParentFile();
                    if (parent != null && !parent.exists()) {
                        parent.mkdirs();
                    }
                    try (InputStream in = getContentResolver().openInputStream(uri);
                         OutputStream out = new FileOutputStream(target)) {
                        if (in == null) {
                            throw new RuntimeException("Could not open selected file.");
                        }
                        byte[] buf = new byte[262144];
                        int len;
                        long lastUpdate = 0L;
                        while ((len = in.read(buf)) > 0) {
                            out.write(buf, 0, len);
                            totalBytes += len;
                            if (totalBytes - lastUpdate >= 32L * 1024L * 1024L) {
                                lastUpdate = totalBytes;
                                final long mb = totalBytes / (1024L * 1024L);
                                runOnUiThread(new Runnable() {
                                    @Override
                                    public void run() {
                                        isoStatus.setText("ISO: Importing... " + mb + " MB");
                                    }
                                });
                            }
                        }
                        out.flush();
                    }
                } catch (Exception e) {
                    error = e.getMessage() != null ? e.getMessage() : e.toString();
                    if (target.exists()) {
                        target.delete();
                    }
                }

                final String finalError = error;
                final long finalMb = totalBytes / (1024L * 1024L);
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        isCopying = false;
                        pickIsoBtn.setEnabled(true);
                        startBtn.setEnabled(true);
                        if (finalError != null) {
                            isoStatus.setText("ISO: Import failed - " + finalError);
                        } else {
                            selectedIsoPath = target.getAbsolutePath();
                            isoStatus.setText("ISO: Ready (" + finalMb + " MB)");
                        }
                    }
                });
            }
        }).start();
    }

    private void writeConfigAndLaunch() {
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .edit()
            .putString(KEY_ISO_PATH, selectedIsoPath)
            .apply();

        File filesDir = getExternalFilesDir(null);
        if (filesDir != null) {
            File conf = new File(filesDir, "gt4recomp_paths.conf");
            try (FileOutputStream out = new FileOutputStream(conf)) {
                StringBuilder sb = new StringBuilder();
                sb.append("ISO=").append(selectedIsoPath).append("\n");
                if (!bundledElfPath.isEmpty()) {
                    sb.append("ELF=").append(bundledElfPath).append("\n");
                }
                out.write(sb.toString().getBytes("UTF-8"));
            } catch (Exception ignored) {
            }
        }

        Intent intent = new Intent(this, NativeActivity.class);
        startActivity(intent);
        finish();
    }
}
