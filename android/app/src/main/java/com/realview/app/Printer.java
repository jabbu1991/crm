package com.realview.app;

import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.BluetoothSocket;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.provider.Settings;

import java.io.IOException;
import java.io.OutputStream;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

/**
 * Sends ready-made ESC/POS bytes (made by the website) to a paired Bluetooth thermal printer.
 * Classic Bluetooth (SPP) first, which most shop printers use; Bluetooth Low Energy for printers that only have that.
 * The chosen printer is remembered on the phone.
 */
@SuppressLint("MissingPermission")   // MainActivity asks for the Bluetooth permission before calling us
class Printer {
    private static final UUID SPP = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB");

    private final MainActivity act;
    private final SharedPreferences prefs;
    private final ExecutorService worker = Executors.newSingleThreadExecutor();

    Printer(MainActivity act) {
        this.act = act;
        this.prefs = act.getSharedPreferences("printer", Context.MODE_PRIVATE);
    }

    String savedName() {
        return prefs.getString("name", "");
    }

    private BluetoothAdapter adapter() {
        BluetoothManager m = (BluetoothManager) act.getSystemService(Context.BLUETOOTH_SERVICE);
        return m == null ? null : m.getAdapter();
    }

    /** Pick one of the printers paired in the phone's Bluetooth settings; then print `thenPrint` if given. */
    void choose(final byte[] thenPrint) {
        BluetoothAdapter ad = adapter();
        if (ad == null) {
            act.toast("This phone has no Bluetooth.");
            return;
        }
        if (!ad.isEnabled()) {
            act.toast("Turn on Bluetooth, then press Print again.");
            try {
                act.startActivity(new Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE));
            } catch (Exception ignored) {
            }
            return;
        }
        Set<BluetoothDevice> bonded = ad.getBondedDevices();
        if (bonded == null || bonded.isEmpty()) {
            new AlertDialog.Builder(act)
                    .setTitle("No paired printer")
                    .setMessage("Pair the printer first: phone Settings → Bluetooth → tap the printer "
                            + "(PIN is usually 0000 or 1234). Then come back and press Print again.")
                    .setPositiveButton("Open Bluetooth settings",
                            (d, w) -> act.startActivity(new Intent(Settings.ACTION_BLUETOOTH_SETTINGS)))
                    .setNegativeButton("Cancel", null)
                    .show();
            return;
        }
        final List<BluetoothDevice> devices = new ArrayList<>(bonded);
        final String[] names = new String[devices.size()];
        for (int i = 0; i < devices.size(); i++) {
            String n = devices.get(i).getName();
            names[i] = (n == null || n.isEmpty()) ? devices.get(i).getAddress() : n;
        }
        new AlertDialog.Builder(act)
                .setTitle("Choose the printer")
                .setItems(names, (d, which) -> {
                    prefs.edit()
                            .putString("addr", devices.get(which).getAddress())
                            .putString("name", names[which])
                            .apply();
                    act.toast("Printer: " + names[which]);
                    if (thenPrint != null) print(thenPrint);
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    void print(final byte[] data) {
        BluetoothAdapter ad = adapter();
        String addr = prefs.getString("addr", null);
        if (ad == null || !ad.isEnabled() || addr == null) {
            choose(data);
            return;
        }
        final BluetoothDevice dev;
        try {
            dev = ad.getRemoteDevice(addr);
        } catch (IllegalArgumentException e) {
            choose(data);
            return;
        }
        worker.execute(() -> {
            String err;
            int type = dev.getType();
            if (type == BluetoothDevice.DEVICE_TYPE_LE) {
                err = printBle(dev, data);
            } else {
                err = printClassic(ad, dev, data);
                if (err != null && type == BluetoothDevice.DEVICE_TYPE_DUAL && printBle(dev, data) == null) err = null;
            }
            final String problem = err;
            act.runOnUiThread(() -> {
                if (problem == null) act.toast("✓ Printed");
                else failed(problem, data);
            });
        });
    }

    private void failed(String problem, final byte[] data) {
        new AlertDialog.Builder(act)
                .setTitle("Could not print")
                .setMessage("Printer: " + savedName() + "\n\nIs the printer switched on and near the phone? "
                        + "Close the RawBT app if it is open.\n\n(" + problem + ")")
                .setPositiveButton("Try again", (d, w) -> print(data))
                .setNeutralButton("Change printer", (d, w) -> choose(data))
                .setNegativeButton("Cancel", null)
                .show();
    }

    // ---------------------------------------------------------------- Classic Bluetooth (SPP)

    private String printClassic(BluetoothAdapter ad, BluetoothDevice dev, byte[] data) {
        try {
            ad.cancelDiscovery();
        } catch (SecurityException ignored) {   // needs the scan permission on new Android; not needed to print
        }
        BluetoothSocket socket = null;
        String firstError = null;
        try {
            socket = dev.createRfcommSocketToServiceRecord(SPP);
            socket.connect();
        } catch (IOException e) {
            firstError = e.getMessage();
            closeQuietly(socket);
            socket = null;
        }
        if (socket == null) {
            try {
                socket = dev.createInsecureRfcommSocketToServiceRecord(SPP);
                socket.connect();
            } catch (IOException e) {
                closeQuietly(socket);
                socket = null;
            }
        }
        if (socket == null) {
            try {   // some cheap printers only answer on channel 1
                Method m = dev.getClass().getMethod("createRfcommSocket", int.class);
                socket = (BluetoothSocket) m.invoke(dev, 1);
                socket.connect();
            } catch (Exception e) {
                closeQuietly(socket);
                return "cannot connect: " + firstError;
            }
        }
        try {
            OutputStream out = socket.getOutputStream();
            for (int i = 0; i < data.length; i += 512) {
                out.write(data, i, Math.min(512, data.length - i));
                out.flush();
                Thread.sleep(15);
            }
            Thread.sleep(Math.min(3000, 400 + data.length / 10));   // let the printer take everything before we close
            return null;
        } catch (Exception e) {
            return "print failed: " + e.getMessage();
        } finally {
            closeQuietly(socket);
        }
    }

    private static void closeQuietly(BluetoothSocket s) {
        if (s == null) return;
        try {
            s.close();
        } catch (IOException ignored) {
        }
    }

    // ---------------------------------------------------------------- Bluetooth Low Energy

    private String printBle(BluetoothDevice dev, byte[] data) {
        BleJob job = new BleJob();
        return job.run(dev, data);
    }

    private class BleJob extends BluetoothGattCallback {
        private final CountDownLatch connected = new CountDownLatch(1);
        private final CountDownLatch discovered = new CountDownLatch(1);
        private final CountDownLatch mtuDone = new CountDownLatch(1);
        private final Semaphore written = new Semaphore(0);
        private volatile boolean lost = false;
        private volatile int mtu = 23;

        String run(BluetoothDevice dev, byte[] data) {
            BluetoothGatt gatt = null;
            try {
                gatt = dev.connectGatt(act, false, this, BluetoothDevice.TRANSPORT_LE);
                if (gatt == null) return "BLE not available";
                if (!connected.await(12, TimeUnit.SECONDS) || lost) return "BLE: printer did not answer";
                gatt.discoverServices();
                if (!discovered.await(10, TimeUnit.SECONDS) || lost) return "BLE: no services";
                BluetoothGattCharacteristic ch = findWritable(gatt);
                if (ch == null) return "BLE: no print channel";
                if (gatt.requestMtu(185)) mtuDone.await(3, TimeUnit.SECONDS);
                int chunk = Math.max(20, mtu - 3);
                boolean noResponse = (ch.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0;
                ch.setWriteType(noResponse ? BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
                        : BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT);
                for (int i = 0; i < data.length; i += chunk) {
                    byte[] part = new byte[Math.min(chunk, data.length - i)];
                    System.arraycopy(data, i, part, 0, part.length);
                    ch.setValue(part);
                    boolean ok = false;
                    for (int tries = 0; tries < 5 && !ok; tries++) {
                        ok = gatt.writeCharacteristic(ch);
                        if (!ok) Thread.sleep(30);
                    }
                    if (!ok || lost) return "BLE: write failed";
                    written.tryAcquire(2, TimeUnit.SECONDS);
                    if (noResponse) Thread.sleep(8);
                }
                Thread.sleep(500);
                return null;
            } catch (InterruptedException e) {
                return "BLE: interrupted";
            } catch (Exception e) {
                return "BLE: " + e.getMessage();
            } finally {
                if (gatt != null) {
                    try {
                        gatt.disconnect();
                        gatt.close();
                    } catch (Exception ignored) {
                    }
                }
            }
        }

        private BluetoothGattCharacteristic findWritable(BluetoothGatt gatt) {
            for (BluetoothGattService s : gatt.getServices()) {
                String id = s.getUuid().toString();
                if (id.startsWith("00001800") || id.startsWith("00001801") || id.startsWith("0000180a")) continue;
                for (BluetoothGattCharacteristic c : s.getCharacteristics()) {
                    int p = c.getProperties();
                    if ((p & (BluetoothGattCharacteristic.PROPERTY_WRITE
                            | BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE)) != 0) {
                        return c;
                    }
                }
            }
            return null;
        }

        @Override
        public void onConnectionStateChange(BluetoothGatt gatt, int status, int newState) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                connected.countDown();
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                lost = true;
                connected.countDown();
                discovered.countDown();
                mtuDone.countDown();
                written.release(1000);
            }
        }

        @Override
        public void onServicesDiscovered(BluetoothGatt gatt, int status) {
            discovered.countDown();
        }

        @Override
        public void onMtuChanged(BluetoothGatt gatt, int newMtu, int status) {
            if (status == BluetoothGatt.GATT_SUCCESS) mtu = newMtu;
            mtuDone.countDown();
        }

        @Override
        public void onCharacteristicWrite(BluetoothGatt gatt, BluetoothGattCharacteristic c, int status) {
            written.release();
        }
    }
}
